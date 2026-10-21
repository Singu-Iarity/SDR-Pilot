"""SDRangel client: REST API control + Remote TCP Sink IQ stream reader.

REST:   http://host:8091/sdrangel/...  (SDRangel's built-in web API)
IQ:     Remote TCP Sink channel plugin (RTL0 or SDRA protocol)
        RTL0: 12-byte header "RTL0" + u32 tuner + u32 gain stages, then u8 IQ
        SDRA: 128-byte header "SDRA" ..., then either raw IQ ("IQ only") or
              framed messages [u8 cmd][u32 BE value-or-length][payload]
"""
from __future__ import annotations

import json
import math
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Callable

import numpy as np

DEVICE_NAMES = {
    0: "Unknown", 1: "RTL-SDR E4000", 2: "RTL-SDR FC0012", 3: "RTL-SDR FC0013", 4: "RTL-SDR FC2580",
    5: "RTL-SDR R820T", 6: "RTL-SDR R828D", 0x80: "Airspy", 0x81: "Airspy HF", 0x82: "Audio input",
    0x83: "BladeRF1", 0x84: "BladeRF2", 0x85: "FCD Pro", 0x86: "FCD Pro+", 0x87: "File input",
    0x88: "HackRF", 0x89: "KiwiSDR", 0x8A: "LimeSDR", 0x8B: "Local input", 0x8C: "Perseus",
    0x8D: "PlutoSDR", 0x8E: "Remote input", 0x8F: "Remote TCP input", 0x90: "SDRplay1",
    0x91: "SDRplay RSP1", 0x92: "SDRplay RSP1A", 0x93: "SDRplay RSP2", 0x94: "SDRplay RSPduo",
    0x95: "SDRplay RSPdx", 0x96: "SigMF file", 0x97: "SoapySDR", 0x98: "Test source",
    0x99: "USRP", 0x9A: "XTRX", 0x9B: "SDRplay RSP1B",
}

# Commands whose 4-byte field is a value (not a payload length)
_VALUE_CMDS = set(range(0x01, 0x0F)) | set(range(0x1F, 0x27)) | {0x40, 0x7E, 0x7F, 0x80} | set(range(0xC0, 0xCA))
CMD_SET_FREQ = 0x01
CMD_SET_RATE = 0x02
CMD_CH_RATE = 0xC3
CMD_CH_OFFSET = 0xC4
CMD_BITS = 0xC6
DATA_IQ, DATA_FLAC, DATA_ZLIB = 0xF0, 0xF1, 0xF2
MSG_TEXT, MSG_BLACKLISTED, DATA_POSITION, DATA_DIRECTION = 0xD0, 0xD1, 0xF3, 0xF4
# SDRangel's Remote TCP Sink writes these messages into the stream even in raw (RTL0 / "IQ only") mode - its station
# position at connect and whenever the position preference changes (with auto-updated "My Position" that's about
# once a second), antenna direction, and text messages. Read as IQ they are bursts of fake full-scale samples
# (broadband clicks on the spectrum) and, when an odd number of bytes, they swap I and Q from then on.
_INBAND_RE = re.compile(rb"[\xf3\xf4]\x00\x00\x00\x0c|\xd1\x00\x00\x00\x00|\xd0\x00\x00[\x00-\x0f]")


class SDRangelError(RuntimeError):
    pass


class SDRangelAPI:
    def __init__(self, host: str, port: int, device_set: int = 0, timeout: float = 5.0):
        self.base = f"http://{host}:{port}/sdrangel"
        self.ds = device_set
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            raise SDRangelError(f"SDRangel API {method} {path} -> HTTP {e.code}: {msg[:300]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SDRangelError(f"Cannot reach SDRangel API at {self.base} ({e}). Is SDRangel running "
                                f"with its web API enabled (default port 8091)?") from None
        return json.loads(raw) if raw.strip() else {}

    # --- instance / device set -------------------------------------------
    def location(self) -> dict:
        """SDRangel's 'My Position' (Preferences): {latitude, longitude}."""
        return self._req("GET", "/sdrangel/location")

    def instance(self) -> dict:
        return self._req("GET", "")

    def device_set(self) -> dict:
        return self._req("GET", f"/deviceset/{self.ds}")

    def device_settings(self) -> dict:
        return self._req("GET", f"/deviceset/{self.ds}/device/settings")

    @staticmethod
    def settings_key(settings: dict) -> str:
        for k, v in settings.items():
            if k.endswith("Settings") and isinstance(v, dict):
                return k
        raise SDRangelError(f"No *Settings block in device settings: {list(settings)}")

    def assert_rx(self, cur: dict | None = None) -> dict:
        """SDR Pilot is receive-only: refuse to touch a transmit (or MIMO) device set."""
        cur = cur if cur is not None else self.device_settings()
        key = next((k for k, v in cur.items() if k.endswith("Settings") and isinstance(v, dict)), "")
        direction = cur.get("direction", 0)
        if direction not in (0, None) or "Output" in key or "MIMO" in key:
            raise SDRangelError(f"device set {self.ds} is a transmitter/MIMO device ({key or direction}). SDR Pilot is "
                                "receive-only and won't control it - pick a receive device set (radio_select_device).")
        return cur

    def patch_device(self, fields: dict) -> dict:
        cur = self.assert_rx()
        key = self.settings_key(cur)
        body = {"deviceHwType": cur.get("deviceHwType"), "direction": cur.get("direction", 0), key: fields}
        return self._req("PATCH", f"/deviceset/{self.ds}/device/settings", body)

    def run_state(self) -> str:
        return self._req("GET", f"/deviceset/{self.ds}/device/run").get("state", "unknown")

    def start(self) -> None:
        self.assert_rx()
        self._req("POST", f"/deviceset/{self.ds}/device/run")

    def stop(self) -> None:
        self.assert_rx()
        self._req("DELETE", f"/deviceset/{self.ds}/device/run")

    def device_report(self) -> dict:
        try:
            return self._req("GET", f"/deviceset/{self.ds}/device/report")
        except SDRangelError:
            return {}  # many devices have no report

    def available_devices(self) -> list[dict]:
        return self._req("GET", "/devices?direction=0").get("devices", []) or []

    def device_sets(self) -> list[dict]:
        return self._req("GET", "/devicesets").get("deviceSets", []) or []

    def select_device(self, item: dict) -> dict:
        body = {k: v for k, v in item.items() if k in ("hwType", "serial", "sequence", "displayedName") and v not in (None, "")}
        body["direction"] = 0
        return self._req("PUT", f"/deviceset/{self.ds}/device", body)

    def add_device_set(self) -> None:
        self._req("POST", "/deviceset?direction=0")

    # --- channels ---------------------------------------------------------
    def channels(self) -> list[dict]:
        return self.device_set().get("channels", []) or []

    def add_channel(self, channel_type: str) -> None:
        self._req("POST", f"/deviceset/{self.ds}/channel", {"channelType": channel_type, "direction": 0})

    def channel_settings(self, index: int) -> dict:
        return self._req("GET", f"/deviceset/{self.ds}/channel/{index}/settings")

    def patch_channel(self, index: int, channel_type: str, fields: dict) -> dict:
        body = {"channelType": channel_type, "direction": 0, f"{channel_type}Settings": fields}
        return self._req("PATCH", f"/deviceset/{self.ds}/channel/{index}/settings", body)

    # --- convenience --------------------------------------------------------
    def summary(self) -> dict:
        ds = self.device_set()
        dev = ds.get("samplingDevice", {})
        settings = self.assert_rx(self.device_settings())
        key = self.settings_key(settings)
        return {
            "hw_type": dev.get("hwType") or settings.get("deviceHwType"),
            "state": dev.get("state"),
            "center_freq_hz": dev.get("centerFrequency"),
            "baseband_rate": dev.get("bandwidth"),
            "settings_key": key,
            "device_settings": settings.get(key, {}),
            "channels": [{"index": c.get("index"), "type": c.get("id"), "title": c.get("title"),
                          "offset_hz": c.get("deltaFrequency")} for c in ds.get("channels", []) or []],
        }


class IQStream(threading.Thread):
    """Reads a Remote TCP Sink stream and hands complex64 blocks to a callback."""

    def __init__(self, host: str, port: int, on_samples: Callable[[np.ndarray], None],
                 on_meta: Callable[[dict], None] | None = None, log: Callable[[str], None] | None = None):
        super().__init__(daemon=True, name="sdrangel-iq")
        self.host, self.port = host, port
        self.on_samples = on_samples
        self.on_meta = on_meta or (lambda m: None)
        self.log = log or (lambda s: None)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.meta: dict = {}
        self.connected = False
        self.error = ""
        self.total_samples = 0
        self.rate_measured = 0.0
        self._rate_t0 = time.time()
        self._rate_n0 = 0
        self.inband_stripped = 0     # SDRangel messages removed from a raw stream
        self.inband_texts: list[str] = []
        self._inband_ref: dict = {}
        self._clean_bytes = 0

    # --- public -------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        s = self._sock
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def send_command(self, cmd: int, value: int) -> bool:
        s = self._sock
        if not s or not self.connected:
            return False
        with self._send_lock:
            try:
                s.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))
                return True
            except OSError:
                return False

    # --- internals ----------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("stream closed by SDRangel")
            buf += chunk
        return bytes(buf)

    def _convert(self, raw: bytes, bits: int) -> np.ndarray:
        if bits == 8:
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            a = (a - 127.5) / 127.5
        elif bits == 16:
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            a = v.astype(np.float32) / 8388608.0
        else:
            a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 8388608.0
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)

    def _emit(self, iq: np.ndarray) -> None:
        if iq.size == 0:
            return
        self.total_samples += iq.size
        now = time.time()
        if now - self._rate_t0 >= 1.0:
            self.rate_measured = (self.total_samples - self._rate_n0) / (now - self._rate_t0)
            self._rate_t0, self._rate_n0 = now, self.total_samples
        self.on_samples(iq)

    def _read_header(self) -> None:
        magic = self._recv_exact(4)
        if magic == b"RTL0":
            rest = self._recv_exact(8)
            dev, stages = struct.unpack(">II", rest)
            self.meta = {"protocol": "RTL0", "device": DEVICE_NAMES.get(dev, hex(dev)), "bits": 8,
                         "iq_only": True}
        elif magic == b"SDRA":
            rest = self._recv_exact(124)
            h = magic + rest
            dev = struct.unpack(">I", h[4:8])[0]
            center = struct.unpack(">Q", h[8:16])[0]
            flags = struct.unpack(">I", h[20:24])[0]
            dev_rate = struct.unpack(">I", h[24:28])[0]
            log2dec = struct.unpack(">I", h[28:32])[0]
            offset = struct.unpack(">i", h[44:48])[0]
            ch_rate = struct.unpack(">I", h[52:56])[0]
            bits = struct.unpack(">I", h[56:60])[0]
            rev = struct.unpack(">I", h[60:64])[0]
            iq_only = not bool((flags >> 7) & 1) if rev >= 1 else True
            self.meta = {"protocol": "SDRA", "device": DEVICE_NAMES.get(dev, hex(dev)), "bits": bits or 8,
                         "iq_only": iq_only, "remote_control": bool((flags >> 6) & 1),
                         "device_center_hz": center, "device_rate": dev_rate, "log2_decim": log2dec,
                         "channel_offset_hz": offset, "channel_rate": ch_rate}
        else:
            raise ConnectionError(f"Unexpected stream header {magic!r} - is this a Remote TCP Sink port?")
        self.on_meta(dict(self.meta))

    def _inband_ok(self, kind: int, payload: bytes, at_start: bool) -> bool:
        """Is this really one of SDRangel's in-band messages (not IQ bytes that happen to look like a header)?
        8-bit IQ can't contain one by chance (it needs three full-scale -1.0 values in a row), so a plausible payload
        is enough there; with 16-32-bit samples it must also match what SDRangel sent right after connecting."""
        strict = self.meta.get("bits", 8) > 8 and not at_start
        if kind == DATA_POSITION:
            lat, lon, alt = struct.unpack(">fff", payload)
            if not (all(math.isfinite(v) for v in (lat, lon, alt)) and abs(lat) <= 90 and abs(lon) <= 180
                    and -1000 <= alt <= 100000):
                return False
            ref = self._inband_ref.get(kind)
            if strict and not (ref and abs(ref[0] - lat) < 0.05 and abs(ref[1] - lon) < 0.05):
                return False
            self._inband_ref[kind] = (lat, lon, alt)
            return True
        if kind == DATA_DIRECTION:
            iso = struct.unpack(">I", payload[:4])[0]
            az, el = struct.unpack(">ff", payload[4:])
            if iso not in (0, 1) or not all(math.isnan(v) or abs(v) <= 720 for v in (az, el)):
                return False
            if strict and self._inband_ref.get(kind) != payload:
                return False
            self._inband_ref[kind] = payload
            return True
        if kind == MSG_TEXT:
            if len(payload) < 3 or payload[0] not in (0, 1) or payload[-1] != 0 or payload.count(0) < 2:
                return False
            try:
                parts = payload[1:].decode("utf-8").split("\0")
            except UnicodeDecodeError:
                return False
            text = " ".join(p for p in parts if p).strip()
            if text:
                self.inband_texts.append(text)
                del self.inband_texts[:-10]
                self.log(f"SDRangel Remote TCP Sink message: {text}")
            return True
        return True  # blacklisted (no payload)

    def _strip_inband(self, data: bytes, pair: int) -> tuple[bytes, bytes]:
        """Remove SDRangel's in-band messages from raw IQ bytes -> (clean bytes, unscanned tail to prepend next time).
        They are written between IQ blocks, so they start on a whole-sample boundary."""
        out = []
        i = pos = 0
        n = len(data)
        hold_from = max(0, n - 4)  # a header split over two reads: keep the tail for the next pass
        while True:
            m = _INBAND_RE.search(data, pos, n)
            if not m:
                break
            p = m.start()
            if p + 5 > n:
                hold_from = min(hold_from, p)
                break
            total = 5 + struct.unpack(">I", data[p + 1:p + 5])[0]
            offset = self._clean_bytes + (p - i)
            if offset % pair:  # not on a sample boundary
                pos = p + 1
                continue
            if p + total > n:
                if total <= 4096:  # the rest of the message hasn't arrived yet
                    hold_from = min(hold_from, p)
                    break
                pos = p + 1
                continue
            if self._inband_ok(data[p], data[p + 5:p + total], offset == 0):
                out.append(data[i:p])
                self._clean_bytes += p - i
                self.inband_stripped += 1
                i = pos = p + total
            else:
                pos = p + 1
        keep = max(i, hold_from)
        out.append(data[i:keep])
        self._clean_bytes += keep - i
        return b"".join(out), data[keep:]

    def _loop_raw(self) -> None:
        bits = self.meta["bits"]
        pair = 2 * bits // 8
        chunk = pair * 32768
        pending = b""
        held = b""
        self._clean_bytes = 0
        while not self._stop.is_set():
            data = self._sock.recv(chunk)
            if not data:
                raise ConnectionError("stream closed by SDRangel")
            clean, held = self._strip_inband(held + data, pair)
            data = pending + clean
            usable = len(data) - (len(data) % pair)
            pending = data[usable:]
            self._emit(self._convert(data[:usable], bits))

    def _loop_framed(self) -> None:
        while not self._stop.is_set():
            hdr = self._recv_exact(5)
            cmd, val = struct.unpack(">BI", hdr)
            if cmd in _VALUE_CMDS:
                key = {CMD_SET_FREQ: "device_center_hz", CMD_SET_RATE: "device_rate", CMD_CH_RATE: "channel_rate",
                       CMD_CH_OFFSET: "channel_offset_hz", CMD_BITS: "bits"}.get(cmd)
                if key:
                    if key == "channel_offset_hz":
                        val = struct.unpack(">i", struct.pack(">I", val))[0]
                    self.meta[key] = val
                    self.on_meta(dict(self.meta))
                continue
            payload = self._recv_exact(val) if val else b""
            if cmd == DATA_ZLIB:
                raw = zlib.decompress(payload)
                self._emit(self._convert(raw, self.meta["bits"]))
            elif cmd == DATA_IQ:
                self._emit(self._convert(payload, self.meta["bits"]))
            elif cmd == DATA_FLAC:
                raise ConnectionError(
                    "SDRangel is sending FLAC-compressed IQ, which SDR Pilot can't decode. In the Remote TCP "
                    "Sink's settings dialog choose compressor 'zlib' or tick 'IQ only' - or set "
                    "sdrangel_protocol to 'rtl0' in sdrpilot_config.json (the default).")
            # messages, position, direction etc. are ignored

    def run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=5)
                self._sock.settimeout(10)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                self._read_header()
                self.connected = True
                self.error = ""
                self.log(f"IQ stream connected ({self.meta.get('protocol')}, {self.meta.get('bits')}-bit, "
                         f"device {self.meta.get('device')})")
                backoff = 0.5
                if self.meta.get("iq_only", True):
                    self._loop_raw()
                else:
                    self._loop_framed()
            except (OSError, ConnectionError, zlib.error, struct.error) as e:
                if self._stop.is_set():
                    break
                self.error = str(e)
                if self.connected:
                    self.log(f"IQ stream lost: {e}")
                self.connected = False
                if "FLAC" in str(e):
                    self._stop.wait(5)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5)
            finally:
                self.connected = False
                try:
                    if self._sock:
                        self._sock.close()
                except OSError:
                    pass
