"""AX.25 / APRS frame parser (GNU Radio embedded Python block).

Input: PDU messages from digital_hdlc_deframer_bp (CRC already checked), e.g. sp_afsk1200_decoder's output.
Prints every frame as SOURCE>DEST,PATH:info (the APRS/TNC2 text format) and appends JSON to out_file."""
import json
import time

import numpy as np
import pmt
from gnuradio import gr


def _call(b):
    name = "".join(chr(c >> 1) for c in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return name + (f"-{ssid}" if ssid else ""), bool(b[6] & 0x01), bool(b[6] & 0x80)


class blk(gr.basic_block):
    def __init__(self, out_file="aprs.jsonl"):
        gr.basic_block.__init__(self, name="AX.25 parser", in_sig=None, out_sig=None)
        self.out_file = out_file
        self.count = 0
        self.message_port_register_in(pmt.intern("frames"))
        self.set_msg_handler(pmt.intern("frames"), self.handle)

    def handle(self, msg):
        try:
            data = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        except Exception:
            return
        if len(data) < 16:
            return
        addrs, i = [], 0
        while i + 7 <= len(data):
            name, last, h = _call(data[i:i + 7])
            addrs.append(name + ("*" if h and i >= 14 else ""))
            i += 7
            if last:
                break
        if len(addrs) < 2 or i + 2 > len(data):
            return
        ctrl, pid = data[i], data[i + 1]
        info = data[i + 2:].decode("latin-1", "replace")
        dest, src, path = addrs[0], addrs[1], addrs[2:]
        line = f"{src}>{dest}" + ("," + ",".join(path) if path else "") + ":" + info
        self.count += 1
        print("SPDATA " + json.dumps({"kind": "aprs", "t": round(time.time(), 3), "key_field": "source", "n": self.count,
                                      "source": src, "dest": dest, "path": path, "info": info, "text": line},
                                     ensure_ascii=False), flush=True)
        try:
            with open(self.out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"n": self.count, "time": time.strftime("%H:%M:%S"), "source": src, "dest": dest,
                                    "path": path, "control": ctrl, "pid": pid, "info": info}) + "\n")
        except OSError:
            pass

    def stop(self):
        print(f"AX.25 summary: {self.count} frames", flush=True)
        return True
