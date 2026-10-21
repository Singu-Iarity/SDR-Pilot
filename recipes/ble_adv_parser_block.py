import json, time
import numpy as np
from gnuradio import gr
import pmt


def _whiten_bits(channel, n):
    w = int(f"{channel:08b}"[::-1], 2) | 2
    out = np.zeros(n, dtype=np.uint8)
    for i in range(n):
        if w & 0x80:
            out[i] = 1
            w ^= 0x11
        w = (w << 1) & 0xFF
    return out


def _crc24(pdu):
    dst = [0x55, 0x55, 0x55]
    for d in pdu:
        for _ in range(8):
            t = dst[0] >> 7
            dst[0] = ((dst[0] << 1) | (dst[1] >> 7)) & 0xFF
            dst[1] = ((dst[1] << 1) | (dst[2] >> 7)) & 0xFF
            dst[2] = (dst[2] << 1) & 0xFF
            if t != (d & 1):
                dst[2] ^= 0x5B
                dst[1] ^= 0x06
            d >>= 1
    return bytes(dst)


PDU_TYPES = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ", 4: "SCAN_RSP",
             5: "CONNECT_IND", 6: "ADV_SCAN_IND", 7: "ADV_EXT_IND"}


class blk(gr.sync_block):
    """BLE advertising packet parser: takes sliced bits (1 per byte) tagged at the end of the
    access address, de-whitens, checks CRC-24 and prints/writes each packet as JSON."""

    def __init__(self, channel=37, tagname="aa", out_file="packets.jsonl"):
        gr.sync_block.__init__(self, name="BLE adv parser", in_sig=[np.uint8], out_sig=None)
        self.channel = int(channel)
        self.tagname = tagname
        self.out_file = out_file
        self.buf = np.zeros(0, dtype=np.uint8)
        self.pending = []  # absolute offsets of packet starts
        self.base = 0      # absolute offset of buf[0]
        self.white = _whiten_bits(self.channel, 8 * 260)
        self.count = 0
        self.ok = 0
        self.f = None  # opened in start(): GRC instantiates this class while validating, so no side effects here

    def start(self):
        if self.out_file:
            self.f = open(self.out_file, "a")
        return True

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        for t in self.get_tags_in_window(0, 0, n, pmt.intern(self.tagname)):
            self.pending.append(t.offset)
        self.buf = np.concatenate([self.buf, x & 1])
        keep_from = len(self.buf)
        still = []
        for off in self.pending:
            i = off - self.base
            if i < 0:
                continue
            if len(self.buf) - i < 16:
                still.append(off); keep_from = min(keep_from, i); continue
            hdr = self.buf[i:i + 16] ^ self.white[:16]
            length = int(np.packbits(hdr[8:16], bitorder="little")[0]) & 0x3F
            total = (2 + length) * 8 + 24
            if len(self.buf) - i < total:
                still.append(off); keep_from = min(keep_from, i); continue
            bits = self.buf[i:i + total] ^ self.white[:total]
            pdu = np.packbits(bits[:(2 + length) * 8], bitorder="little").tobytes()
            crc = np.packbits(bits[(2 + length) * 8:], bitorder="big").tobytes()
            self.report(pdu, crc == _crc24(pdu))
        self.pending = still
        # drop consumed bits but keep enough history for pending packets
        drop = min(keep_from, max(0, len(self.buf) - 2200))
        self.buf = self.buf[drop:]
        self.base += drop
        return n

    def report(self, pdu, crc_ok):
        self.count += 1
        self.ok += crc_ok
        ptype = pdu[0] & 0x0F
        rec = {"n": self.count, "crc_ok": bool(crc_ok), "pdu_type": PDU_TYPES.get(ptype, ptype),
               "tx_random": bool(pdu[0] & 0x40), "len": pdu[1] & 0x3F}
        payload = pdu[2:]
        if ptype in (0, 2, 4, 6) and len(payload) >= 6:
            rec["adv_addr"] = ":".join(f"{b:02X}" for b in payload[:6][::-1])
            ad = payload[6:]
            i = 0
            fields = []
            while i < len(ad):
                L = ad[i]
                if L == 0 or i + 1 + L > len(ad):
                    break
                typ, data = ad[i + 1], ad[i + 2:i + 1 + L]
                if typ in (0x08, 0x09):
                    rec["name"] = data.decode(errors="replace")
                elif typ == 0x01:
                    rec["flags"] = data.hex()
                elif typ == 0xFF:
                    rec["manufacturer"] = {"company_id": f"0x{int.from_bytes(data[:2], 'little'):04X}", "data": data[2:].hex()}
                else:
                    fields.append({"type": f"0x{typ:02X}", "data": data.hex()})
                i += 1 + L
            if fields:
                rec["other_ad"] = fields
        rec["raw_pdu"] = pdu.hex()
        line = json.dumps(rec)
        print("SPDATA " + json.dumps({"kind": "ble", "t": round(time.time(), 3), "key_field": "adv_addr", **rec}),
              flush=True)
        if self.f:
            self.f.write(line + "\n"); self.f.flush()

    def stop(self):
        print(f"BLE summary: {self.count} packets, {self.ok} with valid CRC", flush=True)
        if self.f:
            self.f.close()
        return True
