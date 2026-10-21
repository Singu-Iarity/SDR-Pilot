"""WiFi (802.11) MAC frame parser for SDR Pilot: takes the MAC frames gr-ieee802-11's decode_mac publishes and turns
each into a decoded record (SPDATA line): frame type, addresses, and for beacons / probe responses the network name
(SSID) and channel. Frames with a transmitter address become rows of a per-device table on the dashboard."""
import json
import time

import numpy as np
import pmt
from gnuradio import gr

MGMT = {0: "assoc-req", 1: "assoc-resp", 2: "reassoc-req", 3: "reassoc-resp", 4: "probe-req", 5: "probe-resp",
        8: "beacon", 9: "atim", 10: "disassoc", 11: "auth", 12: "deauth", 13: "action"}
CTRL = {8: "block-ack-req", 9: "block-ack", 10: "ps-poll", 11: "rts", 12: "cts", 13: "ack", 14: "cf-end"}
DATA = {0: "data", 4: "null", 8: "qos-data", 12: "qos-null"}


def _mac(b):
    return ":".join(f"{x:02x}" for x in b)


class blk(gr.basic_block):
    """802.11 MAC frame -> decoded record (type, addresses, SSID/channel of beacons). log_control: also report
    ACK/CTS/RTS frames (busy channels make many of them)."""

    def __init__(self, log_control=False):
        gr.basic_block.__init__(self, name="WiFi MAC parser", in_sig=None, out_sig=None)
        self.log_control = bool(log_control)
        self.count = 0
        self.by_type = {}
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self.handle)

    def handle(self, msg):
        try:
            data = bytes(bytearray(pmt.u8vector_elements(pmt.cdr(msg)))) if pmt.is_pair(msg) else b""
        except Exception:
            return
        if len(data) < 10:
            return
        fc0, fc1 = data[0], data[1]
        ftype, sub = (fc0 >> 2) & 3, (fc0 >> 4) & 15
        kind = {0: "mgmt", 1: "ctrl", 2: "data"}.get(ftype, "ext")
        name = (MGMT if ftype == 0 else CTRL if ftype == 1 else DATA if ftype == 2 else {}).get(sub, f"{kind}-{sub}")
        self.count += 1
        self.by_type[name] = self.by_type.get(name, 0) + 1
        rec = {"kind": "wifi", "t": round(time.time(), 3), "n": self.count, "frame": name, "type": kind,
               "len": len(data), "addr1": _mac(data[4:10])}
        if ftype == 1 and not self.log_control:
            return
        if len(data) >= 16 and not (ftype == 1 and sub in (12, 13)):
            rec["addr2"] = _mac(data[10:16])
            rec["key_field"] = "addr2"
        if len(data) >= 22 and ftype != 1:
            rec["addr3"] = _mac(data[16:22])
        if ftype == 2:
            ds = fc1 & 3
            rec["direction"] = {0: "adhoc", 1: "to AP", 2: "from AP", 3: "WDS"}[ds]
            rec["protected"] = bool(fc1 & 0x40)
        if ftype == 0 and sub in (8, 5) and len(data) >= 36:
            i = 36
            while i + 2 <= len(data):
                tag, ln = data[i], data[i + 1]
                val = data[i + 2:i + 2 + ln]
                if tag == 0:
                    rec["ssid"] = val.decode("utf-8", "replace") if ln else "(hidden)"
                elif tag == 3 and ln == 1:
                    rec["channel"] = val[0]
                i += 2 + ln
            rec["interval_tu"] = int.from_bytes(data[32:34], "little")
        print("SPDATA " + json.dumps(rec, ensure_ascii=False), flush=True)

    def stop(self):
        print(f"WiFi summary: {self.count} frames " + json.dumps(self.by_type), flush=True)
        return True
