"""SDR Pilot data sink (installed into GNU Radio's hier-block folder; used by the 'sp_data_sink' block).

Turns whatever a decoder publishes on a message port - gr-rds parser tuples, gr-adsb / PDU dicts, plain dicts,
strings - into one line of JSON per message on stdout, prefixed with "SPDATA ". SDR Pilot collects those lines from
the job log (decoded.jsonl in the job folder, the job summary, and the dashboard's Decoded view), so decoded data
shows up in a fixed, parseable format whatever decoder produced it."""
import json
import math
import sys
import time

import numpy as np
import pmt
from gnuradio import gr

PREFIX = "SPDATA "
RDS_FIELDS = {0: "pi", 1: "ps", 2: "pty", 3: "flags", 4: "radiotext", 5: "clock", 6: "af"}
RDS_FLAGS = ["tp", "ta", "music", "stereo", "artificial_head", "compressed", "static_pty"]


def _clean(v, depth=0):
    if depth > 6:
        return str(v)
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, 7)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return _clean(float(v))
    if isinstance(v, dict):
        return {str(k): _clean(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        if v and len(v) > 8 and all(isinstance(x, (int, np.integer)) and 0 <= int(x) <= 1 for x in v[:64]):
            return {"bits": "".join(str(int(x)) for x in v[:2048]), "nbits": len(v)}
        if v and len(v) > 8 and all(isinstance(x, (int, np.integer)) and 0 <= int(x) <= 255 for x in v[:64]):
            return {"hex": bytes(int(x) & 255 for x in v[:1024]).hex(), "nbytes": len(v)}
        return [_clean(x, depth + 1) for x in v[:256]]
    return v


def to_record(kind, msg):
    try:
        v = pmt.to_python(msg)
    except Exception:
        v = pmt.write_string(msg)
    rec = {}
    if kind == "rds" and isinstance(v, (tuple, list)) and len(v) == 2 and isinstance(v[0], (int, np.integer)):
        name = RDS_FIELDS.get(int(v[0]), f"type{int(v[0])}")
        text = v[1] if isinstance(v[1], str) else str(_clean(v[1]))
        rec[name] = text.strip() if name in ("ps", "radiotext") else text
        if name == "flags" and isinstance(text, str) and set(text) <= {"0", "1"}:
            rec["flags"] = {f: c == "1" for f, c in zip(RDS_FLAGS, text)}
    elif isinstance(v, (tuple, list)) and len(v) == 2 and isinstance(v[0], dict):  # a PDU: (metadata, data)
        rec.update(_clean(v[0]))
        data = _clean(v[1])
        if data not in (None, [], {}):
            rec["data"] = data
    elif isinstance(v, dict):
        rec.update(_clean(v))
    elif isinstance(v, str):
        rec["text"] = v
    else:
        rec["value"] = _clean(v)
    return rec


class data_sink(gr.basic_block):
    """kind: what the records are ('rds', 'adsb', 'aprs', 'packet', ...); key: the field that identifies one
    station/aircraft/device (e.g. 'icao') so the dashboard can show a table, '' for a plain message log."""

    def __init__(self, kind="data", key=""):
        gr.basic_block.__init__(self, name="sp_data_sink", in_sig=None, out_sig=None)
        self.kind = str(kind or "data")
        self.key = str(key or "")
        self.n = 0
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self.handle)

    def handle(self, msg):
        try:
            rec = to_record(self.kind, msg)
        except Exception as e:  # never let a strange message stop the flowgraph
            rec = {"error": f"unreadable message: {e}"}
        self.n += 1
        out = {"kind": self.kind, "t": round(time.time(), 3), "n": self.n}
        if self.key:
            out["key_field"] = self.key
        out.update(rec)
        try:
            sys.stdout.write(PREFIX + json.dumps(out, default=str, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
