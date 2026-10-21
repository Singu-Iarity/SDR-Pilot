"""Simulated RF environment + a fake SDRangel (REST API + Remote TCP Sink).

Lets the whole plugin be exercised without hardware: the radio code talks to
this exactly as it talks to the real SDRangel.

Simulated emitters (absolute frequencies):
  * 2402 / 2426 / 2480 MHz  BLE advertiser "N1FUR-TEST" (ADV_NONCONN_IND, 100 ms interval)
  * 2437 MHz               Wi-Fi-like wideband bursts (interference)
  * 433.92 MHz             OOK/PWM weather sensor, one packet every ~9 s (intermittent)
  * 100.1 MHz              WBFM broadcast with stereo pilot and RDS (PS 'SDRPILOT' + RadioText)
  * 144.39 MHz             APRS: AFSK1200 AX.25 packets every ~7 s (callsign SIMTST)
  * 162.55 MHz             NBFM tone melody (like a weather radio test)
"""
from __future__ import annotations

import json
import os
import math
import re
import socket
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# ------------------------------------------------------------------ helpers

def erf(x: np.ndarray) -> np.ndarray:
    # Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7)
    s = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-x * x)
    return s * y


def _rng(*key) -> np.random.Generator:
    return np.random.default_rng(abs(hash(key)) % (2 ** 63))


# ------------------------------------------------------------------ BLE packet construction

def swapbits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def ble_crc(pdu: bytes) -> bytes:
    """BLE CRC-24 (init 0x555555 for advertising). Returns 3 bytes, each sent MSB first."""
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


def ble_whitening_bits(channel: int, n: int) -> np.ndarray:
    w = swapbits(channel) | 2
    out = np.zeros(n, dtype=np.uint8)
    for i in range(n):
        if w & 0x80:
            out[i] = 1
            w ^= 0x11
        w = (w << 1) & 0xFF
    return out


def bytes_lsb_first(b: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(b, dtype=np.uint8), bitorder="little")


def ble_adv_bits(channel: int, adv_addr: bytes, ad: bytes) -> np.ndarray:
    """On-air bit sequence (preamble..CRC) for an ADV_NONCONN_IND on an advertising channel."""
    payload = adv_addr[::-1] + ad  # AdvA is sent little-endian
    header = bytes([0x42, len(payload)])  # PDU type 0x2 (ADV_NONCONN_IND), TxAdd=1 (random)
    pdu = header + payload
    crc = ble_crc(pdu)
    pdu_bits = bytes_lsb_first(pdu)
    crc_bits = np.unpackbits(np.frombuffer(crc, dtype=np.uint8), bitorder="big")
    body = np.concatenate([pdu_bits, crc_bits])
    body ^= ble_whitening_bits(channel, len(body))
    preamble = bytes_lsb_first(b"\xAA")
    aa = bytes_lsb_first(struct.pack("<I", 0x8E89BED6))
    return np.concatenate([preamble, aa, body]).astype(np.uint8)


def gfsk_phase(bits: np.ndarray, fs: float, sym_rate: float, dev_hz: float, bt: float = 0.5,
               pad_syms: int = 2) -> np.ndarray:
    """Phase trajectory of a GFSK burst: NRZ impulses convolved with the Gaussian-filtered rect pulse."""
    T = 1.0 / sym_rate
    n = int(round((len(bits) + 2 * pad_syms) * T * fs))
    c = math.pi * bt / T * math.sqrt(2 / math.log(2))
    half = int(math.ceil(2.5 * T * fs))
    tg = np.arange(-half, half + 1) / fs
    g = 0.5 * (erf(c * (tg + T / 2)) - erf(c * (tg - T / 2)))
    imp = np.zeros(n)
    centers = np.round(((np.arange(len(bits)) + pad_syms) * T + T / 2) * fs).astype(int)
    imp[np.clip(centers, 0, n - 1)] = bits.astype(float) * 2 - 1
    freq = np.convolve(imp, g, mode="same")
    return 2 * math.pi * dev_hz * np.cumsum(freq) / fs


# ------------------------------------------------------------------ emitters

class Emitter:
    freq: float = 0.0
    half_bw: float = 0.0

    def render(self, out: np.ndarray, t0: float, fs: float, fc: float) -> None:
        raise NotImplementedError

    def visible(self, fc: float, fs: float) -> bool:
        return abs(self.freq - fc) < fs / 2 + self.half_bw


class BLEAdvertiser(Emitter):
    def __init__(self, name: str = "N1FUR-TEST", interval: float = 0.100, amp: float = 0.25):
        self.name = name
        self.interval = interval
        self.amp = amp
        self.addr = bytes.fromhex("C0FFEE5A1F01")
        self.channels = {37: 2402e6, 38: 2426e6, 39: 2480e6}
        self.freq = 2441e6
        self.half_bw = 40e6
        self._cache: dict = {}

    def ad_data(self, counter: int) -> bytes:
        nm = self.name.encode()
        return (bytes([2, 0x01, 0x06]) + bytes([len(nm) + 1, 0x09]) + nm
                + bytes([5, 0xFF, 0xFF, 0xFF]) + struct.pack("<H", counter & 0xFFFF))

    def _wave(self, fs: float, counter: int, ch: int) -> np.ndarray:
        key = (fs, counter, ch)
        w = self._cache.get(key)
        if w is None:
            bits = ble_adv_bits(ch, self.addr, self.ad_data(counter))
            ph = gfsk_phase(bits, fs, 1e6, 250e3, 0.5)
            ramp = np.ones(len(ph))
            r = max(1, int(4e-6 * fs))
            ramp[:r] = np.linspace(0, 1, r)
            ramp[-r:] = np.linspace(1, 0, r)
            w = (self.amp * ramp * np.exp(1j * ph)).astype(np.complex64)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = w
        return w

    def render(self, out, t0, fs, fc):
        n = len(out)
        t1 = t0 + n / fs
        k0 = int(math.floor((t0 - 0.02) / self.interval))
        k1 = int(math.floor(t1 / self.interval))
        for k in range(k0, k1 + 1):
            delay = _rng("ble", k).uniform(0, 0.010)
            ev = k * self.interval + delay
            for j, (ch, f) in enumerate(self.channels.items()):
                if abs(f - fc) > fs / 2 + 1e6:
                    continue
                ts = ev + j * 0.0006
                w = self._wave(fs, k, ch)
                s = int(round((ts - t0) * fs))
                a, b = max(s, 0), min(s + len(w), n)
                if a >= b:
                    continue
                seg = w[a - s:b - s]
                tt = t0 + np.arange(a, b) / fs
                out[a:b] += seg * np.exp(2j * np.pi * (f - fc) * tt).astype(np.complex64)


class WifiBursts(Emitter):
    def __init__(self, freq=2437e6, bw=16.6e6, amp=0.08):
        self.freq, self.bw, self.amp = freq, bw, amp
        self.half_bw = bw / 2
        self.period = 0.035

    def render(self, out, t0, fs, fc):
        n = len(out)
        t1 = t0 + n / fs
        for k in range(int(t0 // self.period) - 1, int(t1 // self.period) + 1):
            r = _rng("wifi", k)
            if r.random() < 0.35:
                continue
            ts = k * self.period + r.uniform(0, self.period * 0.5)
            dur = r.uniform(0.0002, 0.0018)
            s, e = int((ts - t0) * fs), int((ts + dur - t0) * fs)
            a, b = max(s, 0), min(e, n)
            if b - a < 16:
                continue
            m = b - a
            spec = (r.standard_normal(m) + 1j * r.standard_normal(m))
            f = np.fft.fftfreq(m, 1 / fs) + fc
            spec[(f < self.freq - self.bw / 2) | (f > self.freq + self.bw / 2)] = 0
            out[a:b] += (self.amp * np.fft.ifft(spec) * math.sqrt(m) / math.sqrt(2)).astype(np.complex64) * 0.5


class OOKSensor(Emitter):
    """PWM OOK: 0 = 250us on/750us off, 1 = 750us on/250us off; sync 250us on + 4ms off; 3 repeats."""

    def __init__(self, freq=433.92e6, amp=0.3, sensor_id=0x5A):
        self.freq, self.amp, self.sensor_id = freq, amp, sensor_id
        self.half_bw = 50e3
        self._cache = {}

    def schedule(self, k: int) -> float:
        return k * 9.0 + _rng("ook", k).uniform(0, 2.0)

    def bits(self, k: int) -> np.ndarray:
        temp = int(round((21.5 + 2 * math.sin(k / 7)) * 10))
        hum = 45 + (k % 10)
        word = (self.sensor_id << 28) | (1 << 27) | (1 << 24) | ((temp & 0xFFF) << 12) | (hum << 4)
        nib = sum((word >> (4 * i)) & 0xF for i in range(1, 9)) & 0xF
        word |= nib
        return np.array([(word >> (35 - i)) & 1 for i in range(36)], dtype=np.uint8)

    def envelope(self, k: int, fs: float) -> np.ndarray:
        key = (k, fs)
        if key in self._cache:
            return self._cache[key]
        u = int(round(250e-6 * fs))
        seg = []
        for _ in range(3):
            seg += [np.ones(u), np.zeros(16 * u)]
            for b in self.bits(k):
                seg += [np.ones(3 * u if b else u), np.zeros(u if b else 3 * u)]
        env = np.concatenate(seg).astype(np.float32)
        self._cache = {key: env}
        return env

    def render(self, out, t0, fs, fc):
        n = len(out)
        t1 = t0 + n / fs
        for k in range(int(t0 // 9.0) - 1, int(t1 // 9.0) + 1):
            ts = self.schedule(k)
            env = self.envelope(k, fs)
            s = int(round((ts - t0) * fs))
            a, b = max(s, 0), min(s + len(env), n)
            if a >= b:
                continue
            tt = t0 + np.arange(a, b) / fs
            drift = 18e3  # cheap sensors are off-frequency
            out[a:b] += (self.amp * env[a - s:b - s] * np.exp(2j * np.pi * (self.freq + drift - fc) * tt)).astype(np.complex64)


class FMTones(Emitter):
    """Continuous FM carrier modulated by a tone melody (integer cycles per note keeps phase continuous)."""

    def __init__(self, freq, dev, notes, note_len=0.25, amp=0.2, chord=False):
        self.freq, self.dev, self.amp = freq, dev, amp
        self.half_bw = dev + 15e3
        self.note_len = note_len
        self.chord = chord
        self.notes = [round(f * note_len) / note_len for f in notes]

    def render(self, out, t0, fs, fc):
        n = len(out)
        t = t0 + np.arange(n) / fs
        idx = np.floor(t / self.note_len).astype(np.int64)
        tl = t - idx * self.note_len
        f = np.array(self.notes)[idx % len(self.notes)]
        integ = (1 - np.cos(2 * np.pi * f * tl)) / (2 * np.pi * f)
        if self.chord:
            f2 = f * 1.25
            integ = 0.5 * integ + 0.5 * (1 - np.cos(2 * np.pi * f2 * tl)) / (2 * np.pi * f2)
        phase = 2 * np.pi * self.dev * integ + 2 * np.pi * (self.freq - fc) * t
        out += (self.amp * np.exp(1j * phase)).astype(np.complex64)


def rds_block(info: int, offset: int) -> list[int]:
    """16 info bits + 10-bit checkword (g(x) = x^10+x^8+x^7+x^5+x^4+x^3+1) xor the block's offset word."""
    reg = info << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= 0x5B9 << (i - 10)
    word = (info << 10) | ((reg & 0x3FF) ^ offset)
    return [(word >> (25 - i)) & 1 for i in range(26)]


def rds_bitstream(pi=0x54A8, ps="SDRPILOT", rt="Hello from the SDR Pilot simulator - RDS test station", pty=10):
    """Groups 0A (station name) and 2A (RadioText), differentially encoded, one period."""
    A, B, C, D = 0x0FC, 0x198, 0x168, 0x1B4
    ps = (ps + " " * 8)[:8]
    rt = (rt + "\r" + " " * 64)[:64]
    bits = []
    for i in range(16):
        seg = i % 4
        b2 = (0 << 12) | (0 << 11) | (0 << 10) | (pty << 5) | (0 << 4) | (1 << 3) | ((1 if seg == 3 else 0) << 2) | seg
        for info, off in ((pi, A), (b2, B), (0xE0CD, C), ((ord(ps[2 * seg]) << 8) | ord(ps[2 * seg + 1]), D)):
            bits += rds_block(info, off)
        b2 = (2 << 12) | (0 << 11) | (0 << 10) | (pty << 5) | (0 << 4) | i
        c = rt[4 * i:4 * i + 4]
        for info, off in ((pi, A), (b2, B), ((ord(c[0]) << 8) | ord(c[1]), C), ((ord(c[2]) << 8) | ord(c[3]), D)):
            bits += rds_block(info, off)
    if sum(bits) % 2:  # keep the differentially encoded stream periodic
        bits = bits + bits
    out, prev = [], 0
    for b in bits:
        prev ^= b
        out.append(prev)
    return np.array(out, dtype=np.int8)


class FMBroadcast(FMTones):
    """FM broadcast station: tone 'music' + 19 kHz stereo pilot + 57 kHz RDS (biphase BPSK, 1187.5 bit/s)."""

    def __init__(self, freq, notes, amp=0.3, ps="SDRPILOT", rt="Hello from the SDR Pilot simulator - RDS test station"):
        super().__init__(freq, 75e3, notes, 0.5, amp, chord=True)
        self.half_bw = 110e3
        self.rds = rds_bitstream(ps=ps, rt=rt).astype(np.float64) * 2 - 1
        self._state = None  # (t_end, rds_phase_integral)

    def render(self, out, t0, fs, fc):
        n = len(out)
        t = t0 + np.arange(n) / fs
        idx = np.floor(t / self.note_len).astype(np.int64)
        tl = t - idx * self.note_len
        f = np.array(self.notes)[idx % len(self.notes)]
        f2 = f * 1.25
        audio = 0.5 * (1 - np.cos(2 * np.pi * f * tl)) / (2 * np.pi * f) + 0.5 * (1 - np.cos(2 * np.pi * f2 * tl)) / (2 * np.pi * f2)
        pilot = (1 - np.cos(2 * np.pi * 19e3 * t)) / (2 * np.pi * 19e3)
        k = np.floor(t * 1187.5).astype(np.int64)
        first_half = (t * 1187.5 - k) < 0.5
        sym = self.rds[k % len(self.rds)] * np.where(first_half, 1.0, -1.0)
        rds = sym * np.sin(2 * np.pi * 57e3 * t)
        start = 0.0
        if self._state is not None and abs(self._state[0] - t0) < 2.0 / fs:
            start = self._state[1]
        integ = start + np.cumsum(rds) / fs
        self._state = (t0 + n / fs, float(integ[-1]))
        phase = 2 * np.pi * self.dev * (0.8 * audio + 0.09 * pilot + 0.06 * integ) + 2 * np.pi * (self.freq - fc) * t
        out += (self.amp * np.exp(1j * phase)).astype(np.complex64)


def ax25_frame(src="SIMTST", dest="APRS", path=("WIDE1-1",), info="!3944.50N/10513.00W-SDR Pilot simulator APRS test"):
    """AX.25 UI frame bytes (addresses, control, PID, info) + FCS (CRC-16/X.25, low byte first)."""
    def addr(call, last):
        name, _, ssid = call.partition("-")
        b = bytes((ord(c) << 1) for c in (name + "      ")[:6])
        return b + bytes([0x60 | ((int(ssid or 0) & 0x0F) << 1) | (1 if last else 0)])
    calls = [dest, src] + list(path)
    body = b"".join(addr(c, i == len(calls) - 1) for i, c in enumerate(calls)) + b"\x03\xf0" + info.encode()
    crc = 0xFFFF
    for byte in body:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    crc ^= 0xFFFF
    return body + bytes([crc & 0xFF, crc >> 8])


def hdlc_bits(frame: bytes, preamble_flags=40, tail_flags=4) -> np.ndarray:
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = flag * preamble_flags
    ones = 0
    for byte in frame:
        for i in range(8):
            b = (byte >> i) & 1
            bits.append(b)
            ones = ones + 1 if b else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    bits += flag * tail_flags
    out, level = [], 1  # NRZI: a 0 toggles the tone, a 1 keeps it
    for b in bits:
        if b == 0:
            level ^= 1
        out.append(level)
    return np.array(out, dtype=np.int8)


class APRSBeacon(Emitter):
    """AFSK1200 (Bell 202) AX.25 packets on NBFM, like an APRS station; one packet every ~7 s."""

    def __init__(self, freq=144.39e6, dev=3e3, amp=0.25, period=7.0):
        self.freq, self.dev, self.amp, self.period = freq, dev, amp, period
        self.half_bw = 8e3
        self._cache: dict = {}

    def _packet(self, k: int):
        if k in self._cache:
            return self._cache[k]
        info = f"!3944.50N/10513.00W-SDR Pilot simulator APRS test #{k % 1000}"
        bits = hdlc_bits(ax25_frame(info=info))
        fa = 192000.0
        spb = fa / 1200.0
        n = int(len(bits) * spb)
        idx = np.minimum((np.arange(n) / spb).astype(np.int64), len(bits) - 1)
        tone = np.where(bits[idx] == 1, 1200.0, 2200.0)
        audio = np.sin(np.cumsum(2 * np.pi * tone / fa))
        phase = 2 * np.pi * self.dev * np.cumsum(audio) / fa  # FM: integral of the audio
        self._cache = {k: (fa, phase)}
        return fa, phase

    def render(self, out, t0, fs, fc):
        n = len(out)
        t1 = t0 + n / fs
        for k in range(int(t0 // self.period) - 1, int(t1 // self.period) + 1):
            if k < 0:
                continue
            ts = k * self.period + 1.3
            fa, phase = self._packet(k)
            te = ts + len(phase) / fa
            if te < t0 or ts > t1:
                continue
            a = max(0, int(np.ceil((ts - t0) * fs)))
            b = min(n, int((te - t0) * fs))
            if b <= a:
                continue
            t = t0 + np.arange(a, b) / fs
            ph = np.interp((t - ts) * fa, np.arange(len(phase)), phase)
            out[a:b] += (self.amp * np.exp(1j * (ph + 2 * np.pi * (self.freq - fc) * t))).astype(np.complex64)


class SimEnvironment:
    def __init__(self, noise_rms: float = 0.012):
        scale = [523.25, 587.33, 659.25, 698.46, 783.99, 880.0, 987.77, 1046.5]
        self.emitters: list[Emitter] = [
            BLEAdvertiser(),
            WifiBursts(),
            OOKSensor(),
            FMTones(162.55e6, 5e3, scale + scale[::-1], 0.25, 0.15),
            FMBroadcast(100.1e6, [261.63, 329.63, 392.0, 523.25], 0.3),
            APRSBeacon(144.39e6),
        ]
        self.noise_rms = noise_rms
        self._rng = np.random.default_rng(1)
        n = 1 << 22
        self._noise = ((self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)) / math.sqrt(2)).astype(np.complex64)

    def generate(self, t0: float, n: int, fs: float, fc: float, gain_db: float = 44.0) -> np.ndarray:
        g = 10 ** ((gain_db - 44.0) / 20)
        out = np.zeros(n, dtype=np.complex64)
        for e in self.emitters:
            if e.visible(fc, fs):
                e.render(out, t0, fs, fc)
        out *= g
        nr = self.noise_rms * max(g, 0.25)
        pos = 0
        while pos < n:
            off = int(self._rng.integers(0, len(self._noise) - 1))
            m = min(n - pos, len(self._noise) - off)
            out[pos:pos + m] += np.float32(nr) * self._noise[off:off + m]
            pos += m
        out += 0.004 * g  # small DC spike like a real zero-IF SDR
        return out


# ------------------------------------------------------------------ fake SDRangel

class FakeSDRangel:
    """Implements the subset of SDRangel's REST API and Remote TCP Sink that SDR Pilot uses."""

    def __init__(self, api_port: int = 18091, center: float = 2402e6, rate: float = 4e6, iq_port: int = 11234):
        self.api_port = api_port
        self.iq_port = iq_port
        self.env = SimEnvironment()
        self.lock = threading.RLock()
        self.dev = {"centerFrequency": int(center), "devSampleRate": int(rate), "log2Decim": 0,
                    "lnaGain": 24, "vgaGain": 20, "lnaExt": 0, "bandwidth": int(rate * 0.75),
                    "biasT": 0, "dcBlock": 0, "iqCorrection": 0, "LOppmTenths": 0, "fcPos": 2}
        self.running = True
        self.channels: list[dict] = []
        self._tcp: dict[int, threading.Thread] = {}
        self._stop = threading.Event()
        self.epoch = time.time()
        self.httpd: ThreadingHTTPServer | None = None

    # --- derived -----------------------------------------------------------
    def baseband_rate(self) -> int:
        return int(self.dev["devSampleRate"] / (2 ** int(self.dev.get("log2Decim", 0))))

    def gain_db(self) -> float:
        return float(self.dev["lnaGain"]) + float(self.dev["vgaGain"]) + (14 if self.dev.get("lnaExt") else 0)

    def device_set(self) -> dict:
        return {"samplingDevice": {"index": 0, "hwType": "HackRF", "direction": 0, "deviceNbStreams": 1,
                                   "centerFrequency": self.dev["centerFrequency"], "bandwidth": self.baseband_rate(),
                                   "state": "running" if self.running else "idle"},
                "channelcount": len(self.channels),
                "channels": [{"index": i, "id": c["type"], "uid": 1000 + i, "title": c["settings"].get("title", "Remote TCP sink"),
                              "deltaFrequency": c["settings"].get("inputFrequencyOffset", 0), "direction": 0}
                             for i, c in enumerate(self.channels)]}

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeSDRangel":
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                fake._route(self, "GET")

            def do_POST(self):
                fake._route(self, "POST")

            def do_PATCH(self):
                fake._route(self, "PATCH")

            def do_PUT(self):
                fake._route(self, "PATCH")

            def do_DELETE(self):
                fake._route(self, "DELETE")

        ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
        for port in range(self.api_port, self.api_port + 20):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
                self.api_port = port
                break
            except OSError:
                continue
        else:
            raise OSError(f"simulator: no free port near {self.api_port}")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-sdrangel-api").start()
        return self

    def shutdown(self) -> None:
        self._stop.set()
        if self.httpd:
            self.httpd.shutdown()

    def _route(self, h, method: str) -> None:
        p = h.path.split("?")[0].rstrip("/")
        try:
            with self.lock:
                if p == "/sdrangel" and method == "GET":
                    return h._send(200, {"appname": "SDRangel (SDR Pilot simulator)", "version": "7.sim",
                                         "devicesetlist": {"devicesetcount": 1}})
                if p == "/sdrangel/devices" and method == "GET":
                    return h._send(200, {"devicecount": 2, "devices": [
                        {"displayedName": "HackRF[0] (simulated)", "hwType": "HackRF", "serial": "sim0001", "sequence": 0,
                         "direction": 0, "deviceNbStreams": 1, "deviceSetIndex": 0, "index": 0},
                        {"displayedName": "TestSource[0]", "hwType": "TestSource", "serial": "", "sequence": 0,
                         "direction": 0, "deviceNbStreams": 1, "deviceSetIndex": -1, "index": 1}]})
                if p == "/sdrangel/devicesets" and method == "GET":
                    return h._send(200, {"devicesetcount": 1, "devicesetfocus": 0, "deviceSets": [self.device_set()]})
                m = re.fullmatch(r"/sdrangel/deviceset/0(/.*)?", p)
                if not m:
                    return h._send(404, {"message": f"no route {p}"})
                sub = m.group(1) or ""
                if sub == "" and method == "GET":
                    return h._send(200, self.device_set())
                if sub == "/device/settings":
                    if method == "PATCH":
                        body = h._body()
                        upd = body.get("hackRFInputSettings", {})
                        for k, v in upd.items():
                            if k in self.dev or k in ("transverterMode",):
                                self.dev[k] = v
                    return h._send(200, {"deviceHwType": "HackRF", "direction": 0, "hackRFInputSettings": dict(self.dev)})
                if sub == "/device/report":
                    return h._send(200, {"deviceHwType": "HackRF", "direction": 0})
                if sub == "/device" and method in ("PATCH", "PUT"):
                    body = h._body()
                    if body.get("hwType") not in (None, "HackRF"):
                        return h._send(404, {"message": f"simulator only has a HackRF, not {body.get('hwType')}"})
                    return h._send(202, {"hwType": "HackRF", "displayedName": "HackRF[0] (simulated)", "direction": 0})
                if sub == "/device/run":
                    if method == "POST":
                        self.running = True
                    elif method == "DELETE":
                        self.running = False
                    return h._send(200, {"state": "running" if self.running else "idle"})
                if sub == "/channel" and method == "POST":
                    body = h._body()
                    t = body.get("channelType")
                    if t != "RemoteTCPSink":
                        return h._send(400, {"message": f"simulator only supports RemoteTCPSink, not {t}"})
                    self.channels.append({"type": t, "settings": {
                        "channelSampleRate": 48000, "inputFrequencyOffset": 0, "gain": 0, "sampleBits": 8,
                        "dataAddress": "0.0.0.0", "dataPort": self.iq_port, "protocol": 1, "title": "Remote TCP sink"}})
                    self._ensure_server(len(self.channels) - 1)
                    return h._send(202, {"message": "channel added"})
                m2 = re.fullmatch(r"/channel/(\d+)/settings", sub)
                if m2:
                    i = int(m2.group(1))
                    if i >= len(self.channels):
                        return h._send(404, {"message": "no such channel"})
                    c = self.channels[i]
                    if method == "PATCH":
                        upd = h._body().get("RemoteTCPSinkSettings", {})
                        old_port = c["settings"]["dataPort"]
                        c["settings"].update(upd)
                        if c["settings"]["dataPort"] != old_port:
                            self._ensure_server(i)
                    return h._send(200, {"channelType": c["type"], "direction": 0, "RemoteTCPSinkSettings": dict(c["settings"])})
                return h._send(404, {"message": f"no route {p}"})
        except Exception as e:  # pragma: no cover
            h._send(500, {"message": str(e)})

    # --- Remote TCP Sink -----------------------------------------------------
    def _ensure_server(self, index: int) -> None:
        port = int(self.channels[index]["settings"]["dataPort"])
        if port in self._tcp:
            return
        t = threading.Thread(target=self._serve_tcp, args=(index, port), daemon=True, name=f"fake-tcp-{port}")
        self._tcp[port] = t
        t.start()

    def _serve_tcp(self, index: int, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
        except OSError as e:
            import sys
            print(f"simulator: cannot listen on IQ port {port}: {e}", file=sys.stderr, flush=True)
            self._tcp.pop(port, None)
            return
        srv.listen(4)
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._client, args=(index, conn), daemon=True).start()
        srv.close()

    def _client(self, index: int, conn: socket.socket) -> None:
        ch = self.channels[index]["settings"]
        proto = int(ch.get("protocol", 1))
        bits = 8 if proto == 0 else int(ch.get("sampleBits", 8))
        iq_only = proto == 0
        try:
            if proto == 0:
                conn.sendall(b"RTL0" + struct.pack(">II", 0x88, 1))
            else:
                h = bytearray(128)
                h[0:4] = b"SDRA"
                struct.pack_into(">I", h, 4, 0x88)
                struct.pack_into(">Q", h, 8, int(self.dev["centerFrequency"]))
                iq_only = bool(int(ch.get("iqOnly", 0) or 0))
                struct.pack_into(">I", h, 20, (0 if iq_only else (1 << 7)) | (1 << 6))  # framed unless IQ only
                struct.pack_into(">I", h, 24, int(self.dev["devSampleRate"]))
                struct.pack_into(">i", h, 44, int(ch.get("inputFrequencyOffset", 0)))
                struct.pack_into(">I", h, 52, int(ch.get("channelSampleRate", 48000)))
                struct.pack_into(">I", h, 56, bits)
                struct.pack_into(">I", h, 60, 1)
                conn.sendall(bytes(h))
            conn.settimeout(0.0)
            t_sent = time.time() - self.epoch  # stream time (s)
            wall0 = time.time()
            stream_t0 = t_sent
            last_fs = None
            while not self._stop.is_set():
                self._drain_commands(conn)
                with self.lock:
                    fs = float(ch.get("channelSampleRate") or self.baseband_rate())
                    fc = float(self.dev["centerFrequency"] + int(ch.get("inputFrequencyOffset", 0)))
                    gain = self.gain_db()
                    running = self.running
                if fs != last_fs:
                    wall0, stream_t0, last_fs = time.time(), t_sent, fs
                due = (time.time() - wall0) - (t_sent - stream_t0)
                if due < 0.01 or not running:
                    time.sleep(0.005)
                    continue
                n = int(min(due, 0.05) * fs)
                if n <= 0:
                    time.sleep(0.002)
                    continue
                x = self.env.generate(t_sent, n, fs, fc, gain)
                t_sent += n / fs
                payload = self._encode(x, bits)
                if proto == 0 or iq_only:
                    data = payload
                else:
                    z = zlib.compress(payload, 1)
                    data = struct.pack(">BI", 0xF2, len(z)) + z
                conn.setblocking(True)
                conn.sendall(data)
                conn.setblocking(False)
        except (OSError, BrokenPipeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _drain_commands(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(4096)
        except (BlockingIOError, socket.timeout):
            return
        except OSError:
            raise
        if not data:
            raise OSError("client closed")
        for i in range(0, len(data) - 4, 5):
            cmd, val = struct.unpack(">BI", data[i:i + 5])
            with self.lock:
                if cmd == 0x01:
                    self.dev["centerFrequency"] = val
                elif cmd == 0x02:
                    self.dev["devSampleRate"] = val

    @staticmethod
    def _encode(x: np.ndarray, bits: int) -> bytes:
        if bits == 8:
            v = np.empty(2 * len(x), dtype=np.float32)
            v[0::2], v[1::2] = x.real, x.imag
            return np.clip(np.round(v * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()
        v = np.empty(2 * len(x), dtype=np.float32)
        v[0::2], v[1::2] = x.real, x.imag
        if bits == 16:
            return np.clip(np.round(v * 32767), -32768, 32767).astype("<i2").tobytes()
        iv = np.clip(np.round(v.astype(np.float64) * 8388607), -8388608, 8388607).astype("<i4")
        if bits == 32:  # SDRangel: 24-bit values in a 32-bit little-endian container
            return iv.tobytes()
        return iv.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()  # 24-bit little-endian
