"""Burst bit dumper (GNU Radio embedded Python block) for FSK/GFSK/ASK links after a slicer.

Input: one bit per byte (digital_binary_slicer_fb output) with stream tags from
digital_correlate_access_code_tag_xx marking the end of the sync word / preamble.
For each tag it takes the next `nbits` bits and prints them as hex (MSB-first and LSB-first per byte), optionally
Manchester-decoded, and appends a JSON line to out_file. Use it to see a protocol's raw frames, then extend the
parsing (length field, CRC, IDs) right here in GNU Radio Companion."""
import json
import time

import numpy as np
import pmt
from gnuradio import gr


def _hex(bits, lsb_first=False):
    b = np.asarray(bits[: len(bits) // 8 * 8], dtype=np.uint8).reshape(-1, 8)
    if lsb_first:
        b = b[:, ::-1]
    return bytes(np.packbits(b, axis=1).ravel()).hex()


class blk(gr.sync_block):
    def __init__(self, tagname="sync", nbits=256, manchester=False, max_print=40, out_file="bursts.jsonl"):
        gr.sync_block.__init__(self, name="Burst bits", in_sig=[np.uint8], out_sig=None)
        self.tagname = tagname
        self.nbits = int(nbits)
        self.manchester = bool(manchester)
        self.max_print = int(max_print)
        self.out_file = out_file
        self.buf = np.zeros(0, dtype=np.uint8)
        self.base = 0
        self.pending = []
        self.count = 0

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        start = self.nitems_read(0)
        for t in self.get_tags_in_window(0, 0, n):
            if pmt.symbol_to_string(t.key) == self.tagname:
                self.pending.append(t.offset)
        self.buf = np.concatenate([self.buf, x.astype(np.uint8) & 1])
        while self.pending and self.pending[0] + self.nbits <= start + n:
            off = self.pending.pop(0)
            i = off - self.base
            if i >= 0:
                self._emit(self.buf[i:i + self.nbits])
        keep = max(0, len(self.buf) - self.nbits - 8)
        if self.pending:
            keep = min(keep, max(0, self.pending[0] - self.base))
        self.buf = self.buf[keep:]
        self.base += keep
        return n

    def _emit(self, bits):
        self.count += 1
        rec = {"n": self.count, "time": time.strftime("%H:%M:%S"), "bits": len(bits),
               "hex_msb": _hex(bits), "hex_lsb": _hex(bits, True)}
        if self.manchester:
            pairs = bits[: len(bits) // 2 * 2].reshape(-1, 2)
            ok = pairs[:, 0] != pairs[:, 1]
            n_ok = int(np.argmin(ok)) if not ok.all() else len(ok)
            rec["manchester_hex"] = _hex(pairs[:n_ok, 0])
            rec["manchester_valid_bits"] = n_ok
        if self.count <= self.max_print:
            print("SPDATA " + json.dumps({"kind": "burst", "t": round(time.time(), 3), **rec}), flush=True)
        try:
            with open(self.out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def stop(self):
        print(f"Burst summary: {self.count} bursts after the sync word", flush=True)
        return True
