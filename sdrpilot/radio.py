"""Radio manager: owns the SDRangel connection, the live IQ ring buffer, captures,
the ZeroMQ fan-out that feeds GNU Radio flowgraphs, and the background monitor."""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from datetime import datetime

import numpy as np

from . import deadline
from . import dsp
from .config import Config
from .sdrangel import CMD_SET_FREQ, CMD_SET_RATE, IQStream, SDRangelAPI, SDRangelError
from .status import StatusBoard

SETTLE_S = 0.3  # discard this much IQ after a retune (SDRangel / TCP pipeline latency)


class RingBuffer:
    def __init__(self, size: int):
        self.buf = np.zeros(size, dtype=np.complex64)
        self.size = size
        self.total = 0  # samples ever written
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)

    def write(self, x: np.ndarray) -> None:
        orig = len(x)
        if orig == 0:
            return
        with self.cv:
            if orig >= self.size:
                x = x[-self.size:]
            n = len(x)
            p = self.total % self.size
            k = min(n, self.size - p)
            self.buf[p:p + k] = x[:k]
            if k < n:
                self.buf[: n - k] = x[k:]
            self.total += orig
            self.cv.notify_all()

    def read(self, start_total: int, n: int) -> np.ndarray:
        """Copy samples [start_total, start_total+n). Caller ensures they are still in the ring."""
        with self.lock:
            if start_total < self.total - self.size:
                raise ValueError("requested samples already overwritten")
            p = start_total % self.size
            k = min(n, self.size - p)
            out = np.empty(n, dtype=np.complex64)
            out[:k] = self.buf[p:p + k]
            if k < n:
                out[k:] = self.buf[: n - k]
            return out

    def wait_until(self, total: int, timeout: float) -> bool:
        end = time.time() + timeout
        with self.cv:
            while self.total < total:
                deadline.check()  # the tool call's own time limit (timeout_s)
                rem = end - time.time()
                if rem <= 0:
                    return False
                self.cv.wait(min(rem, 0.5))
            return True


class Publisher:
    """GNU Radio flowgraph: python queue source -> ZMQ PUB sink (one producer, many flowgraph subscribers)."""

    def __init__(self, address: str, log):
        self.base_address = address
        self.address = address
        self.log = log
        self.refs = 0
        self.tb = None
        self.src = None
        self.lock = threading.Lock()
        self.error = ""

    def _build(self):
        from gnuradio import gr, zeromq  # imported lazily: GR startup is slow

        q: collections.deque = collections.deque()
        cv = threading.Condition()

        class QueueSource(gr.sync_block):
            def __init__(self):
                gr.sync_block.__init__(self, name="sdrpilot_live_iq", in_sig=None, out_sig=[np.complex64])
                self.pending = None
                self.backlog = 0

            def push(self, x):
                with cv:
                    q.append(x)
                    self.backlog += len(x)
                    while self.backlog > 32_000_000 and q:  # drop if subscribers can't keep up
                        self.backlog -= len(q.popleft())
                    cv.notify()

            def work(self, input_items, output_items):
                out = output_items[0]
                n = len(out)
                produced = 0
                with cv:
                    if self.pending is None and not q:
                        cv.wait(0.1)
                    while produced < n:
                        if self.pending is None:
                            if not q:
                                break
                            self.pending = q.popleft()
                            self.backlog -= len(self.pending)
                        k = min(n - produced, len(self.pending))
                        out[produced:produced + k] = self.pending[:k]
                        produced += k
                        self.pending = self.pending[k:] if k < len(self.pending) else None
                return produced

        tb = gr.top_block("sdrpilot_fanout")
        src = QueueSource()
        host, port = self.base_address.rsplit(":", 1)
        last = None
        for p in range(int(port), int(port) + 20):  # another SDR Pilot copy may already own the port
            addr = f"{host}:{p}"
            try:
                sink = zeromq.pub_sink(gr.sizeof_gr_complex, 1, addr, 100, False, -1, "", True, True)
                self.address = addr
                break
            except Exception as e:  # address in use
                last = e
        else:
            raise RuntimeError(f"no free ZeroMQ port near {self.base_address}: {last}")
        tb.connect(src, sink)
        return tb, src

    def acquire(self) -> None:
        with self.lock:
            self.refs += 1
            if self.tb is None:
                try:
                    self.tb, self.src = self._build()
                    self.tb.start()
                    self.log(f"IQ fan-out started on {self.address}")
                except Exception as e:  # pragma: no cover
                    self.error = str(e)
                    self.tb = self.src = None
                    raise

    def release(self) -> None:
        with self.lock:
            self.refs = max(0, self.refs - 1)
            if self.refs == 0 and self.tb is not None:
                tb, src = self.tb, self.src  # keep the Python block alive until the flowgraph has stopped
                self.src = None
                try:
                    tb.stop()
                    tb.wait()
                except Exception:
                    pass
                self.tb = None
                del src
                self.log("IQ fan-out stopped (no flowgraphs using the live radio)")

    def push(self, x: np.ndarray) -> None:
        src = self.src
        if src is not None:
            src.push(x)

    @property
    def active(self) -> bool:
        return self.tb is not None


class Monitor:
    """Watches the live stream for transmissions that come and go."""

    def __init__(self, radio: "Radio"):
        self.radio = radio
        self.thread: threading.Thread | None = None
        self.stop_ev = threading.Event()
        self.detections: collections.deque = collections.deque(maxlen=2000)
        self.params: dict = {}
        self.started = None
        self.blocks = 0
        self.lock = threading.Lock()

    def start(self, threshold_db=10.0, min_duration_ms=0.0, freq_min_hz=None, freq_max_hz=None,
              merge_gap_ms=5.0, nfft=512) -> None:
        self.stop()
        self.params = {"threshold_db": threshold_db, "min_duration_ms": min_duration_ms, "freq_min_hz": freq_min_hz,
                       "freq_max_hz": freq_max_hz, "merge_gap_ms": merge_gap_ms, "nfft": nfft}
        self.stop_ev.clear()
        self.started = time.time()
        self.blocks = 0
        with self.lock:
            self.detections.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="sdrpilot-monitor")
        self.thread.start()
        self._lease = self.radio.lease.hold("signal monitor", center=self.radio.center_freq, rate=self.radio.sample_rate)

    def stop(self) -> None:
        self.stop_ev.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.thread = None
        if getattr(self, "_lease", None):
            self.radio.lease.release(self._lease)
            self._lease = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _run(self) -> None:
        r = self.radio
        ring = r.ring
        pos = ring.total
        floor_bin = None
        epoch = r.epoch
        p = self.params
        while not self.stop_ev.is_set():
            fs = r.sample_rate
            if not fs:
                deadline.sleep(0.2)
                continue
            block = int(fs * 0.25)
            if not ring.wait_until(pos + block, 1.0):
                continue
            if r.epoch != epoch:  # retuned: reset noise model
                epoch = r.epoch
                floor_bin = None
                pos = max(ring.total - block, r.settle_mark)
                continue
            if ring.total - pos > ring.size - block:
                pos = ring.total - block  # fell behind: skip
            if pos < r.settle_mark:
                pos = r.settle_mark
                continue
            x = ring.read(pos, block)
            t_block = time.time() - (ring.total - pos) / fs
            pos += block
            fc = r.center_freq
            evs, info = dsp.find_events(x, fs, fc, nfft=p["nfft"], threshold_db=p["threshold_db"],
                                        merge_gap_s=p["merge_gap_ms"] / 1e3, floor_bin=floor_bin)
            bfb = info.get("block_floor_bin")
            if bfb is not None:
                floor_bin = bfb if floor_bin is None else 0.9 * floor_bin + 0.1 * bfb
            self.blocks += 1
            for e in evs:
                if e.get("already_on_at_start") and e.get("still_on_at_end"):
                    continue  # continuous during this block: not intermittent
                if e["duration_s"] * 1e3 < p["min_duration_ms"]:
                    continue
                if p["freq_min_hz"] and e["freq_hz"] < p["freq_min_hz"]:
                    continue
                if p["freq_max_hz"] and e["freq_hz"] > p["freq_max_hz"]:
                    continue
                det = dict(e)
                det["time"] = round(t_block + e["start_s"], 4)
                det["time_str"] = datetime.fromtimestamp(det["time"]).strftime("%H:%M:%S.%f")[:-3]
                det.pop("start_s", None)
                with self.lock:
                    # a burst that straddles two analysis blocks arrives in two pieces: join them
                    merged = False
                    for prev in list(self.detections)[-6:]:
                        gap = det["time"] - (prev["time"] + prev["duration_s"])
                        if -0.001 <= gap <= p["merge_gap_ms"] / 1e3 + 0.002 and \
                                abs(det["freq_hz"] - prev["freq_hz"]) < max(10e3, 0.5 * min(det["bandwidth_hz"], prev["bandwidth_hz"])):
                            prev["duration_s"] = round(det["time"] + det["duration_s"] - prev["time"], 6)
                            prev["snr_db"] = max(prev["snr_db"], det["snr_db"])
                            prev["bandwidth_hz"] = max(prev["bandwidth_hz"], det["bandwidth_hz"])
                            merged = True
                            break
                    if not merged:
                        self.detections.append(det)
            r.board.set_radio(monitor={"running": True, "detections": len(self.detections), "blocks": self.blocks})

    def summary(self, last_n: int = 40) -> dict:
        with self.lock:
            dets = list(self.detections)
        groups: list[dict] = []
        for d in sorted(dets, key=lambda d: d["freq_hz"]):
            if groups:
                gbw = float(np.median([i["bandwidth_hz"] for i in groups[-1]["items"]]))
                tol = max(10e3, 0.25 * min(d["bandwidth_hz"], gbw))
            if groups and abs(d["freq_hz"] - groups[-1]["_f"]) < tol:
                g = groups[-1]
            else:
                g = {"_f": d["freq_hz"], "items": []}
                groups.append(g)
            g["items"].append(d)
            g["_f"] = float(np.mean([i["freq_hz"] for i in g["items"]]))
        out = []
        for g in groups:
            it = sorted(g["items"], key=lambda d: d["time"])
            times = [d["time"] for d in it]
            out.append({
                "freq_hz": round(g["_f"], 1),
                "count": len(it),
                "first_seen": it[0]["time_str"],
                "last_seen": it[-1]["time_str"],
                "typical_duration_ms": round(float(np.median([d["duration_s"] for d in it])) * 1e3, 3),
                "typical_bandwidth_hz": round(float(np.median([d["bandwidth_hz"] for d in it])), 1),
                "max_snr_db": max(d["snr_db"] for d in it),
                "median_interval_s": round(float(np.median(np.diff(times))), 3) if len(times) > 1 else None,
            })
        out.sort(key=lambda g: -g["count"])
        return {"running": self.running, "params": self.params,
                "watching_s": round(time.time() - self.started, 1) if self.started else 0,
                "transmitters": out, "latest": dets[-last_n:]}


class RadioBusy(RuntimeError):
    pass


class RadioLease:
    """Advertises to other SDR Pilot copies (other chats) that this one is using the radio right now.

    A small JSON file in the workspace, renewed every few seconds while anything here needs the current
    tuning (live flowgraphs, monitor, captures). Expired leases are ignored, so a crashed copy never
    blocks the radio for long."""

    TTL = 15.0

    def __init__(self, path: str):
        self.path = path
        self.reasons: dict[int, str] = {}
        self._n = 0
        self.lock = threading.Lock()
        self.info: dict = {}
        threading.Thread(target=self._renew_loop, daemon=True, name="sdrpilot-lease").start()

    def hold(self, reason: str, **info) -> int:
        with self.lock:
            self._n += 1
            self.reasons[self._n] = reason
            self.info.update(info)
            self._write()
            return self._n

    def release(self, key: int) -> None:
        with self.lock:
            self.reasons.pop(key, None)
            if not self.reasons:
                cur = self._read()
                if cur and cur.get("pid") == os.getpid():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
            else:
                self._write()

    def other_holder(self) -> dict | None:
        cur = self._read()
        if cur and cur.get("pid") != os.getpid() and cur.get("expires", 0) > time.time():
            return cur
        return None

    def _read(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self) -> None:
        other = self.other_holder()
        if other:
            return  # don't overwrite another copy's lease; tune() already warned/forced
        data = {"pid": os.getpid(), "reasons": sorted(set(self.reasons.values())), "expires": time.time() + self.TTL,
                "since": datetime.now().strftime("%H:%M:%S"), **self.info}
        tmp = self.path + f".{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _renew_loop(self) -> None:
        while True:
            deadline.sleep(5)
            with self.lock:
                if self.reasons:
                    self._write()


class Radio:
    def __init__(self, cfg: Config, board: StatusBoard):
        self.cfg = cfg
        self.board = board
        self.api: SDRangelAPI | None = None
        self.stream: IQStream | None = None
        self.fake = None
        self.mode = None
        self.center_freq = 0.0
        self.sample_rate = 0.0
        self.channel_index = None
        self.hw_type = ""
        self.epoch = 0
        self.settle_mark = 0
        self.last_meta: dict = {}
        self.retunes: list[dict] = []
        size = int(min(cfg.ring_max_samples, max(4_000_000, 20e6 * cfg.ring_seconds)))
        self.ring = RingBuffer(size)
        self.publisher = Publisher(cfg.zmq_address, lambda m: board.note(m, "event"))
        os.makedirs(os.path.join(cfg.workspace, "state"), exist_ok=True)
        self.lease = RadioLease(os.path.join(cfg.workspace, "state", "radio_lease.json"))
        self.monitor = Monitor(self)
        from .watch import SpectrumRecorder
        self.recorder = SpectrumRecorder(self)
        self._capture_lock = threading.Lock()
        self._capture: dict | None = None
        self._conn_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None

    # ----------------------------------------------------------- tuning made elsewhere
    def _watch_tuning(self) -> None:
        """Another SDR Pilot copy (another chat, the dashboard agent) or SDRangel's own window can retune the radio
        while this copy keeps reading the same IQ stream. Without noticing, everything here - the dashboard's
        frequency axis, spectrum_watch, captures - would label the new samples with the old frequency. A cheap
        poll of SDRangel's device set catches it within about half a second."""
        while True:
            deadline.sleep(0.5)
            api = self.api
            if api is None or self.mode is None or not self.connected:
                continue
            try:
                dev = (api.device_set() or {}).get("samplingDevice") or {}
                fc = float(dev.get("centerFrequency") or 0)
                fs = float(dev.get("bandwidth") or 0)
            except Exception:
                continue
            if fc and fs and (fc != self.center_freq or fs != self.sample_rate):
                old = (self.center_freq, self.sample_rate)
                self.center_freq, self.sample_rate = fc, fs
                self._mark_retune()
                self.board.note(f"radio retuned elsewhere: {old[0] / 1e6:.4f} MHz / {old[1] / 1e6:g} MS/s -> "
                                f"{fc / 1e6:.4f} MHz / {fs / 1e6:g} MS/s", "event")

    # ----------------------------------------------------------- connection
    @property
    def connected(self) -> bool:
        return bool(self.stream and self.stream.connected)

    paused = False  # set from the Instances page: this copy leaves the radio alone until resumed

    def pause(self) -> None:
        """Stop reading the IQ stream (frees the PC's time and the network for other copies) until resume()."""
        self.paused = True
        try:
            self.recorder.stop()
        except Exception:  # pragma: no cover
            pass
        self.disconnect(keep_sim=True)
        self.board.set_radio(state="paused")

    def resume(self) -> dict:
        self.paused = False
        st = self.connect(force=True)
        self.board.set_radio(state="connected")
        return st

    def ensure(self) -> None:
        if self.paused:
            raise SDRangelError("this SDR Pilot copy was PAUSED by the user (Instances page on the dashboard) so it "
                                "doesn't use the radio. Tell the user what you want to do and ask them to resume it - "
                                "don't try to work around it.")
        if self.api is None or self.stream is None:
            self.connect(force=False)
        elif not self.stream.connected:
            # give a reconnect a moment
            end = time.time() + 3
            while time.time() < end and not self.stream.connected:
                deadline.sleep(0.1)
            if not self.stream.connected:
                raise SDRangelError(f"IQ stream not connected: {self.stream.error or 'waiting for SDRangel'}")

    def connect(self, mode: str | None = None, force: bool = True) -> dict:
        with self._conn_lock:
            mode = mode or self.cfg.source
            if not force and self.stream is not None and self.stream.connected and mode == self.mode:
                return self.status()  # another caller connected while we waited for the lock
            self.disconnect(keep_sim=(mode == "sim"))
            self.mode = mode
            if mode == "sim":
                from .sim import FakeSDRangel
                if self.fake is None:
                    self.fake = FakeSDRangel(self.cfg.sim_api_port, self.cfg.sim_center_freq, self.cfg.sim_sample_rate,
                                             self.cfg.sim_iq_port).start()
                host, api_port, iq_port, ds = "127.0.0.1", self.fake.api_port, self.cfg.sim_iq_port, 0
            else:
                if self.fake:
                    self.fake.shutdown()
                    self.fake = None
                host, api_port = self.cfg.sdrangel_host, self.cfg.sdrangel_api_port
                iq_port, ds = self.cfg.sdrangel_iq_port, self.cfg.sdrangel_device_set
            self.api = SDRangelAPI(host, api_port, ds)
            try:
                inst = self.api.instance()
            except SDRangelError:
                if mode == "sim" or not self._start_sdrangel():
                    raise
                inst = self.api.instance()
            if self.api.run_state() != "running":
                try:
                    self.api.start()
                    deadline.sleep(0.5)
                except SDRangelError:
                    pass
            state = self.api.run_state()
            if state != "running":
                raise SDRangelError(f"SDRangel device set {ds} is '{state}', not running. Select your SDR in "
                                    "SDRangel and press its start (play) button, then try again.")
            self._refresh()
            self._setup_channel(iq_port)
            self.stream = IQStream(host, iq_port, self._on_samples, self._on_meta,
                                   log=lambda m: self.board.note(m, "event"))
            self.stream.start()
            end = time.time() + 6
            while time.time() < end and not self.stream.connected:
                deadline.sleep(0.05)
            if not self.stream.connected:
                raise SDRangelError(f"Could not open the IQ stream on {host}:{iq_port}: "
                                    f"{self.stream.error or 'timeout'}")
            self._mark_retune()
            self.recorder.start()
            if self._watch_thread is None or not self._watch_thread.is_alive():
                self._watch_thread = threading.Thread(target=self._watch_tuning, daemon=True,
                                                      name="sdrpilot-tuning-watch")
                self._watch_thread.start()
            self.board.set_radio(state="connected", mode=mode, hw=self.hw_type, center_freq=self.center_freq,
                                 sample_rate=self.sample_rate, sdrangel=inst.get("version"))
            return self.status()

    def _start_sdrangel(self) -> bool:
        """Standalone use: SDRangel isn't running - start it (if allowed and found) and wait for its web API."""
        if not self.cfg.sdrangel_autostart or self.cfg.sdrangel_host not in ("127.0.0.1", "localhost"):
            return False
        import shutil
        import subprocess
        cands = [self.cfg.sdrangel_exe] if self.cfg.sdrangel_exe else []
        if os.name == "nt":
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                         os.path.expandvars(r"%LocalAppData%\Programs")):
                if base:
                    cands += [os.path.join(base, "SDRangel", "sdrangel.exe"), os.path.join(base, "sdrangel", "sdrangel.exe")]
        cands += [shutil.which("sdrangel") or "", shutil.which("sdrangelsrv") or ""]
        exe = next((c for c in cands if c and os.path.exists(c)), None)
        if not exe:
            return False
        self.board.note(f"SDRangel wasn't running - starting {os.path.basename(exe)}", "event")
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([exe], cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **kw)
        end = time.time() + 40
        while time.time() < end:
            try:
                self.api.instance()
                deadline.sleep(2)  # let it finish loading its device sets
                return True
            except SDRangelError:
                deadline.sleep(1)
        return False

    def disconnect(self, keep_sim: bool = False) -> None:
        self.monitor.stop()
        if self.stream:
            self.stream.stop()
            self.stream = None
        if self.fake and not keep_sim:
            self.fake.shutdown()
            self.fake = None
        self.api = None
        self.board.set_radio(state="disconnected")

    def _setup_channel(self, iq_port: int) -> None:
        chans = [c for c in self.api.channels() if c.get("id") == "RemoteTCPSink"]
        if not chans:
            if not self.cfg.auto_configure_channel:
                raise SDRangelError("No Remote TCP Sink channel in SDRangel. Add one (Rx channels > Remote TCP sink) "
                                    "or set auto_configure_channel true.")
            try:
                self.api.add_channel("RemoteTCPSink")
            except SDRangelError as e:
                raise SDRangelError(f"SDRangel refused to add a Remote TCP Sink channel ({e}). This SDRangel may be too "
                                    "old to have it - update to a current release - or add it by hand: "
                                    "Rx channels > Remote TCP sink.") from None
            end = time.time() + 5
            while time.time() < end:
                chans = [c for c in self.api.channels() if c.get("id") == "RemoteTCPSink"]
                if chans:
                    break
                deadline.sleep(0.2)
            if not chans:
                raise SDRangelError("Asked SDRangel to add a Remote TCP Sink channel but it did not appear.")
            self.board.note("Added a Remote TCP Sink channel in SDRangel", "event")
        self.channel_index = chans[0]["index"]
        if self.cfg.auto_configure_channel:
            fmt = self.stream_format()
            proto = 0 if fmt["protocol"] == "rtl0" else 1
            fields = {"channelSampleRate": int(self.sample_rate), "inputFrequencyOffset": 0, "gain": 0,
                      "dataPort": int(iq_port), "protocol": proto, "sampleBits": fmt["bits"],
                      "maxSampleRate": max(int(self.sample_rate), 20_000_000)}
            if proto == 1:  # SDRangel protocol: raw IQ, or zlib (FLAC, its default, can't be read here)
                fields["iqOnly"] = 1 if fmt["compression"] == "none" else 0
                if fmt["compression"] == "zlib":
                    fields["compression"] = 1
            if self.mode != "sim" and self.cfg.sdrangel_host in ("127.0.0.1", "localhost"):
                fields["dataAddress"] = "127.0.0.1"  # don't expose the IQ stream to the network
            cur = self.api.channel_settings(self.channel_index).get("RemoteTCPSinkSettings", {})
            changed = {k: v for k, v in fields.items() if cur.get(k) != v}
            if changed:
                self.api.patch_channel(self.channel_index, "RemoteTCPSink", changed)
                deadline.sleep(0.3)

    def stream_format(self) -> dict:
        proto = "sdra" if str(self.cfg.sdrangel_protocol).lower() in ("sdra", "sdrangel") else "rtl0"
        bits = int(self.cfg.sdrangel_bits or 0)
        if proto == "rtl0":
            bits = 8
        elif bits not in (8, 16, 24, 32):
            bits = 16
        comp = "zlib" if proto == "sdra" and str(self.cfg.sdrangel_compression).lower() == "zlib" else "none"
        return {"protocol": proto, "bits": bits, "compression": comp}

    def set_stream_format(self, protocol: str = "", bits: int = 0, compression: str = "") -> dict:
        """Switch the Remote TCP Sink's IQ format and reconnect. If no samples flow in the new format within a few
        seconds the previous format is restored."""
        old = (self.cfg.sdrangel_protocol, self.cfg.sdrangel_bits, self.cfg.sdrangel_compression)
        p = (protocol or self.stream_format()["protocol"]).lower()
        if p in ("sdrangel", "sdra"):
            p = "sdra"
        elif p in ("rtl0", "rtl_tcp", "rtltcp"):
            p = "rtl0"
        elif p in ("wss", "sdra_wss", "sdrangel wss", "sdrangel_wss"):
            raise ValueError("'SDRangel wss' is the same protocol wrapped in WebSocket + TLS for SDRangel's browser "
                             "(WebAssembly) version - extra overhead and certificates for nothing here. Use 'sdra'.")
        else:
            raise ValueError(f"protocol {protocol!r}: use 'sdra' (SDRangel protocol, 8/16/24/32-bit) or 'rtl0' (8-bit)")
        b = int(bits or (16 if p == "sdra" else 8))
        if p == "rtl0" and b != 8:
            raise ValueError("rtl0 is always 8-bit - use protocol 'sdra' for 16/24/32-bit samples")
        if b not in (8, 16, 24, 32):
            raise ValueError("bits must be 8, 16, 24 or 32")
        c = (compression or ("none" if p == "rtl0" else self.cfg.sdrangel_compression or "none")).lower()
        if c not in ("none", "zlib"):
            raise ValueError("compression: 'none' (raw IQ - best on the same PC) or 'zlib' (SDRangel on another "
                             "machine over a slow link). FLAC can't be read by SDR Pilot")
        if p == "rtl0":
            c = "none"
        self.cfg.sdrangel_protocol, self.cfg.sdrangel_bits, self.cfg.sdrangel_compression = p, b, c
        try:
            self._apply_stream_format()
        except Exception as e:
            self.cfg.sdrangel_protocol, self.cfg.sdrangel_bits, self.cfg.sdrangel_compression = old
            try:
                self._apply_stream_format()
            except Exception:  # pragma: no cover
                pass
            raise SDRangelError(f"switching the IQ stream to {p} {b}-bit failed ({e}) - kept "
                                f"{old[0]} {old[1] or ''}") from None
        try:
            from . import config as _config
            _config.save_settings(sdrangel_protocol=p, sdrangel_bits=b, sdrangel_compression=c)
        except Exception:  # pragma: no cover
            pass
        return self.status()

    def _apply_stream_format(self) -> None:
        if self.api is None:
            self.connect(force=False)
            return
        host = "127.0.0.1" if self.mode == "sim" else self.cfg.sdrangel_host
        iq_port = self.cfg.sim_iq_port if self.mode == "sim" else self.cfg.sdrangel_iq_port
        if self.stream:
            self.stream.stop()
            self.stream = None
        self._setup_channel(iq_port)
        deadline.sleep(0.3)
        self.stream = IQStream(host, iq_port, self._on_samples, self._on_meta, log=lambda m: self.board.note(m, "event"))
        self.stream.start()
        end = time.time() + 6
        n0 = None
        while time.time() < end:
            if self.stream.connected:
                if n0 is None:
                    n0 = self.stream.total_samples
                elif self.stream.total_samples - n0 > max(10000, self.sample_rate * 0.2):
                    break
            deadline.sleep(0.05)
        else:
            raise SDRangelError(self.stream.error or "no samples arrived")
        want = self.stream_format()
        got = self.stream.meta
        if got.get("protocol", "").lower() != want["protocol"] or int(got.get("bits") or 8) != want["bits"]:
            raise SDRangelError(f"SDRangel sent {got.get('protocol')} {got.get('bits')}-bit instead (an older "
                                "SDRangel without this option?)")
        self._mark_retune()

    def _refresh(self) -> dict:
        s = self.api.summary()
        self.hw_type = s.get("hw_type") or ""
        fc = float(s.get("center_freq_hz") or 0)
        fs = float(s.get("baseband_rate") or 0)
        changed = (fc != self.center_freq) or (fs != self.sample_rate)
        self.center_freq, self.sample_rate = fc, fs
        self.last_summary = s
        if changed and self.stream is not None:
            self._mark_retune()
        return s

    def _mark_retune(self) -> None:
        self.epoch += 1
        self.settle_mark = self.ring.total + int(max(self.sample_rate, 1) * SETTLE_S)
        self.retunes.append({"t": time.time(), "center": self.center_freq, "rate": self.sample_rate})
        self._retune_t = time.time()
        del self.retunes[:-50]
        self.board.set_radio(center_freq=self.center_freq, sample_rate=self.sample_rate)

    # ----------------------------------------------------------- samples
    def _on_meta(self, meta: dict) -> None:
        self.last_meta = meta

    def _on_samples(self, iq: np.ndarray) -> None:
        self.ring.write(iq)
        if self.publisher.active:
            self.publisher.push(iq)
        cap = self._capture
        if cap is not None:
            self._capture_feed(cap, iq)

    def fresh(self, seconds: float, timeout: float | None = None) -> np.ndarray:
        """Wait for and return `seconds` of IQ captured after any pending retune settles."""
        self.ensure()
        self._refresh()
        fs = self.sample_rate
        n = int(seconds * fs)
        if n > self.ring.size // 2:
            raise ValueError(f"{seconds}s at {fs / 1e6:.2f} MS/s is too long for the live buffer; use capture_iq instead")
        start = max(self.ring.total, self.settle_mark)
        to = timeout if timeout is not None else seconds * 3 + 3
        if not self.ring.wait_until(start + n, to):
            got = self.ring.total - start
            raise SDRangelError(f"IQ stream too slow: got {max(got, 0)} of {n} samples in {to:.1f}s "
                                f"(measured {self.stream.rate_measured / 1e6:.2f} MS/s vs {fs / 1e6:.2f} MS/s expected)")
        return self.ring.read(start, n)

    def latest(self, seconds: float) -> np.ndarray | None:
        fs = self.sample_rate
        if not fs or not self.connected:
            return None
        n = min(int(seconds * fs), self.ring.total - self.settle_mark, self.ring.size // 2)
        if n <= 1024:
            return None
        return self.ring.read(self.ring.total - n, n)

    # ----------------------------------------------------------- control
    def check_free(self, center_freq_hz: float | None, sample_rate: float | None, force: bool) -> str | None:
        """Refuse to retune under another chat's live work unless forced. Returns a warning when forced."""
        other = self.lease.other_holder()
        if not other:
            return None
        changes = (center_freq_hz is not None and abs(center_freq_hz - self.center_freq) > 1) or \
                  (sample_rate is not None and abs(sample_rate - self.sample_rate) > 1)
        if not changes:
            return None
        what = ", ".join(other.get("reasons") or ["live work"])
        msg = (f"another SDR Pilot session (another chat, pid {other.get('pid')}) is using the radio for: {what} "
               f"at {float(other.get('center', 0)) / 1e6:.4f} MHz since {other.get('since')}")
        if not force:
            raise RadioBusy(msg + ". Retuning now would break its results. Wait for it to finish, work at the "
                                  "current tuning, or call again with force=true if the user says so.")
        return "forced retune: " + msg

    def tune(self, center_freq_hz: float | None = None, sample_rate: float | None = None, force: bool = False) -> dict:
        self.ensure()
        warning = self.check_free(center_freq_hz, sample_rate, force)
        from . import devices
        fields: dict = {}
        notes = []
        dev = self.last_summary.get("device_settings", {})
        key = self.last_summary.get("settings_key", "")
        if center_freq_hz is not None:
            if "centerFrequency" not in dev:
                raise ValueError(f"{self.hw_type or key} has no tunable centre frequency")
            fields["centerFrequency"] = int(round(center_freq_hz))
        if sample_rate is not None:
            sr_fields, note = devices.sample_rate_plan(key, dev, self.api.device_report(), sample_rate)
            fields.update(sr_fields)
            if note:
                notes.append(note)
        if not fields:
            return self.status()
        try:
            self.api.patch_device(fields)
        except SDRangelError:
            # fall back to rtl_tcp-style commands over the stream (works when Remote control is enabled)
            if center_freq_hz is not None:
                self.stream.send_command(CMD_SET_FREQ, int(center_freq_hz))
            if sample_rate is not None:
                self.stream.send_command(CMD_SET_RATE, int(sample_rate))
        deadline.sleep(0.15)
        self._refresh()
        if sample_rate is not None and self.cfg.auto_configure_channel and self.channel_index is not None:
            try:
                self.api.patch_channel(self.channel_index, "RemoteTCPSink",
                                       {"channelSampleRate": int(self.sample_rate),
                                        "maxSampleRate": max(int(self.sample_rate), 20_000_000)})
            except SDRangelError:
                pass
        self._mark_retune()
        st = self.status()
        if warning:
            st["warning"] = warning
        if notes:
            st["sample_rate_note"] = "; ".join(notes)
        if sample_rate is not None and abs(self.sample_rate - sample_rate) > 0.05 * sample_rate:
            st["sample_rate_warning"] = (f"asked for {sample_rate:g} S/s, the device delivers {self.sample_rate:g} S/s "
                                         "(closest it supports)")
        bw = (st.get("device_settings") or {}).get("bandwidth")
        if bw is not None:
            st["filter_bandwidth_hz"] = bw  # analog/baseband filter actually in effect (HackRF BBF etc.)
        if center_freq_hz is not None and abs(self.center_freq - center_freq_hz) > 1000:
            st["center_freq_warning"] = (f"asked for {center_freq_hz:g} Hz, the device reports {self.center_freq:g} Hz "
                                         "(out of range, or an LO/fcPos/transverter offset is set)")
        return st

    def run_device(self, running: bool, force: bool = False) -> dict:
        """The SDRangel device's start/stop button."""
        if self.api is None:
            self.connect(force=False)
        other = self.lease.other_holder()
        if not running and other and not force:
            raise RadioBusy(f"another SDR Pilot session (pid {other.get('pid')}) is using the radio for: "
                            f"{', '.join(other.get('reasons') or ['live work'])}. Stopping the SDR would cut its stream. "
                            "Call again with force=true only if the user says so.")
        before = self.api.run_state()
        if running:
            if before != "running":
                self.api.start()
            end = time.time() + 8
            while time.time() < end and not (self.stream is not None and self.stream.connected):
                deadline.sleep(0.2)
        elif before == "running":
            self.api.stop()
            deadline.sleep(0.3)
        after = self.api.run_state()
        self.board.note(f"SDR {'started' if running else 'stopped'} (was {before}, now {after})", "event")
        self.board.set_radio(state="connected" if running and after == "running" else "stopped by request")
        out = {"device_state_before": before, "device_state": after,
               "stream_connected": bool(self.stream is not None and self.stream.connected)}
        if running and after == "running":
            try:
                out.update(self.status())
            except Exception:
                pass
        return out

    def set_device(self, fields: dict) -> dict:
        self.ensure()
        dev = self.last_summary.get("device_settings", {})
        unknown = [k for k in fields if k not in dev]
        if unknown and dev:
            from . import devices
            raise ValueError(f"{self.hw_type} has no setting(s) {unknown}. Valid: "
                             + ", ".join(sorted(devices.field_docs(self.last_summary.get('settings_key', ''), dev))))
        self.api.patch_device(fields)
        deadline.sleep(0.15)
        self._refresh()
        self._mark_retune()
        if int(fields.get("iqCorrection") or 0):
            deadline.sleep(0.6)  # give the correction loop a moment, then check it didn't run away
        st = self.status()
        st["applied"] = {k: st.get("device_settings", {}).get(k) for k in fields}
        if int(fields.get("iqCorrection") or 0):
            st["iq_correction_note"] = ("SDRangel's IQ correction can run away (especially at low gain) and pin the "
                                        "stream at a constant full-scale value; SDR Pilot switches it off again if "
                                        "that happens. Its benefit on a HackRF is small - prefer leaving it off.")
        return st

    def set_gain(self, gain_db: float | None, auto: bool | None) -> dict:
        from . import devices
        self.ensure()
        self._refresh()
        key = self.last_summary.get("settings_key", "")
        fields, note = devices.gain_plan(key, self.last_summary.get("device_settings", {}),
                                         self.api.device_report(), gain_db, auto)
        st = self.set_device(fields)
        st["gain_note"] = note
        st["gain_stages"] = devices.gain_guide(key)["this_device"]
        return st

    def device_info(self) -> dict:
        from . import devices
        self.ensure()
        s = self._refresh()
        key = s.get("settings_key", "")
        report = self.api.device_report()
        rb = devices.report_body(report)
        info = {"hw_type": s.get("hw_type"), "settings_key": key, "device_set": self.api.ds,
                "center_freq_hz": self.center_freq, "baseband_rate": self.sample_rate,
                "settings": devices.field_docs(key, s.get("device_settings", {})),
                "report": rb, "supported_sample_rates": devices.supported_rates(report) or None}
        try:
            info["available_devices"] = [{k: d.get(k) for k in ("displayedName", "hwType", "serial", "sequence", "deviceSetIndex")}
                                         for d in self.api.available_devices()]
        except SDRangelError:
            pass
        try:
            info["device_sets"] = [{"index": i, "hw_type": d.get("samplingDevice", {}).get("hwType"),
                                    "state": d.get("samplingDevice", {}).get("state")}
                                   for i, d in enumerate(self.api.device_sets())]
        except SDRangelError:
            pass
        try:
            devices.gain_plan(key, s.get("device_settings", {}), report, 20, None)
            info["radio_set_gain"] = "supported for this device"
        except ValueError as e:
            info["radio_set_gain"] = str(e)
        info["gain_guide"] = devices.gain_guide(key)
        return info

    def select_device(self, hw_type: str = "", serial: str = "", sequence: int | None = None,
                      displayed_name: str = "", device_set: int | None = None) -> dict:
        """Point SDR Pilot at another SDR: switch device set and/or the SDR used in it, start it, re-stream."""
        if device_set is not None:
            self.cfg.sdrangel_device_set = int(device_set)
        if hw_type or serial or displayed_name or sequence is not None:
            api = SDRangelAPI(self.cfg.sdrangel_host if self.mode != "sim" else "127.0.0.1",
                              self.cfg.sdrangel_api_port if self.mode != "sim" else (self.fake.api_port if self.fake else self.cfg.sim_api_port),
                              self.cfg.sdrangel_device_set)
            try:
                api.stop()
            except SDRangelError:
                pass
            api.select_device({"hwType": hw_type, "serial": serial, "sequence": sequence, "displayedName": displayed_name})
            deadline.sleep(1.0)
            try:
                api.start()
            except SDRangelError:
                pass
            deadline.sleep(0.5)
        return self.connect(self.mode, force=True)

    def status(self) -> dict:
        st: dict = {"mode": self.mode, "connected": self.connected}
        if self.api is not None:
            try:
                s = self._refresh()
                st.update({"hw_type": s["hw_type"], "device_state": s["state"],
                           "center_freq_hz": self.center_freq, "sample_rate": self.sample_rate,
                           "device_settings": s["device_settings"], "channels": s["channels"]})
            except SDRangelError as e:
                st["api_error"] = str(e)
        if self.stream is not None:
            st["stream"] = {"connected": self.stream.connected, "protocol": self.stream.meta.get("protocol"),
                            "bits": self.stream.meta.get("bits"), "measured_rate": round(self.stream.rate_measured),
                            "error": self.stream.error or None}
            if getattr(self.stream, "inband_stripped", 0):
                st["stream"]["sdrangel_messages_removed"] = self.stream.inband_stripped
        st["zmq_fanout"] = {"address": self.cfg.zmq_address, "active": self.publisher.active,
                            "flowgraphs_using": self.publisher.refs}
        st["monitor_running"] = self.monitor.running
        try:
            fixed = self.repair_stuck()
            if fixed:
                st["stream_repaired"] = fixed
            lvl = self.signal_level()
            if lvl:
                st["signal_level"] = lvl
            rn = self.receiver_note()
            if rn:
                st["receiver"] = rn
        except Exception:  # pragma: no cover
            pass
        self.board.set_radio(state="connected" if self.connected else "disconnected", center_freq=self.center_freq,
                             sample_rate=self.sample_rate, hw=self.hw_type, mode=self.mode,
                             measured_rate=st.get("stream", {}).get("measured_rate"))
        return st

    # ----------------------------------------------------------- receiver quirks (DC spike)
    def receiver_note(self) -> dict | None:
        """What the AI should know about this SDR, e.g. a DC spike at the exact centre (zero-IF receivers)."""
        from . import receivers
        s = self.last_summary or {}
        key = s.get("settings_key", "")
        note = receivers.info(key, self.hw_type or s.get("hw_type", ""))
        if not note:
            return None
        dev = s.get("device_settings") or {}
        if key == "sdrPlayV3Settings" and "ifFrequencyIndex" in dev:
            zero = int(dev.get("ifFrequencyIndex") or 0) == 0
            note["dc_spike_at_center"] = zero
            note["advice"] = ("Zero-IF mode (ifFrequencyIndex 0): DC spike at the centre - tune off-centre, or switch "
                              "to a low-IF mode." if zero else "Low-IF mode: no centre DC spike.")
        if note["dc_spike_at_center"] is True and int(dev.get("dcBlock") or 0):
            rest = note["advice"].split("Leave iqCorrection", 1)
            note["advice"] = ("dcBlock is on, so the centre spike is already removed - no need to tune away from it. "
                              "Only a very narrow signal (a few kHz) sitting exactly on the centre would lose anything."
                              + (" Leave iqCorrection" + rest[1] if len(rest) > 1 else ""))
            note["dc_block_on"] = True
        if note["dc_spike_at_center"] is True and self.center_freq:
            off = receivers.lo_offset(key, dev)
            note["dc_spike_hz"] = self.center_freq + off
            if off:
                note["dc_spike_note"] = (f"fcPos/decimation put the LO {off / 1e3:+.0f} kHz from the displayed centre, so "
                                         f"the DC spike sits at {(self.center_freq + off) / 1e6:.4f} MHz "
                                         + ("(outside the band you receive)" if abs(off) >= self.sample_rate / 2 else ""))
        if self.center_freq and self.sample_rate:
            sp = receivers.known_spurs(key, self.center_freq - self.sample_rate / 2, self.center_freq + self.sample_rate / 2)
            if sp:
                note["known_spurs_in_band_hz"] = sp
        return note

    def dc_spike_freq(self) -> float | None:
        n = self.receiver_note()
        if not n or n.get("dc_spike_at_center") is not True:
            return None
        return float(n.get("dc_spike_hz") or self.center_freq)

    def stream_health(self) -> dict:
        """Cheap check for the dashboard (every few seconds): is the stream pinned/stalled, too quiet to be useful,
        or arriving slower than the sample rate (samples being lost)? A problem must show in two checks in a row
        (three for a stalled stream) before it's reported - one quiet 50 ms slice is not a fault."""
        now = time.time()
        if now - getattr(self, "_health_t", 0) < 3:
            return getattr(self, "_health", {})
        self._health_t = now
        h: dict = {}
        streak = getattr(self, "_health_streak", {})
        try:
            lvl = self.signal_level(0.05) if self.connected else None
            v = (lvl or {}).get("verdict")
            for k in ("stuck", "too_quiet", "frozen"):
                hit = v == k or (k == "frozen" and lvl and lvl.get("frozen_window"))
                streak[k] = streak.get(k, 0) + 1 if hit else 0
            if v == "stuck" and streak["stuck"] >= 2:
                h = {"stuck": True, "advice": lvl.get("advice"),
                     "message": "The IQ stream is pinned at a constant value - this spectrum is not real. "
                                + (lvl.get("advice") or "")}
                fixed = self.repair_stuck(lvl)
                if fixed:
                    h["repaired"] = fixed
                    h["message"] = fixed
            elif streak.get("frozen", 0) >= 3:
                h = {"stalled": True,
                     "message": "The IQ stream has been delivering exactly the ADC's zero level for several seconds "
                                "- no noise at all. The SDR may have stopped (USB) or SDRangel isn't passing samples: "
                                "check SDRangel's device is running and the gain isn't at 0."}
            elif v == "too_quiet" and streak["too_quiet"] >= 2:
                h = {"quiet": True, "advice": lvl.get("advice"),
                     "message": "Gain too low for the 8-bit stream: most samples sit on the zero level, so weak "
                                "signals are lost. The spectrum is real - raise the gain (VGA, then LNA)."}
            st = self.stream
            fs = float(self.sample_rate or 0)
            if st is not None and st.connected and fs and st.rate_measured and \
                    now - getattr(self, "_retune_t", 0) > 5 and st.rate_measured < 0.85 * fs:
                h["slow"] = {"measured": round(st.rate_measured), "expected": fs}
                h.setdefault("message", "")
                h["message"] = (h["message"] + " " if h["message"] else "") + (
                    f"The IQ stream is arriving at only {st.rate_measured / 1e6:.1f} of {fs / 1e6:g} MS/s - this PC "
                    "isn't keeping up, so samples are lost (gaps and lines in the waterfall, broken decodes). Each "
                    "SDR Pilot copy (every chat plus the background service) reads its own copy of the stream: lower "
                    "the sample rate if the signal doesn't need it, or close chats you aren't using.")
        except Exception:  # pragma: no cover
            pass
        self._health_streak = streak
        self._health = h
        return h

    def repair_stuck(self, lvl: dict | None = None) -> str | None:
        """The stream is stuck at a constant value while SDRangel's IQ correction is on: switch it off (it's what
        pins the samples) and say so. At most once a minute."""
        if time.time() - getattr(self, "_repair_t", 0) < 60 or self.api is None:
            return None
        lvl = lvl or self.signal_level(0.2)
        if not lvl or lvl.get("verdict") != "stuck":
            return None
        dev = (self.last_summary or {}).get("device_settings") or {}
        if not int(dev.get("iqCorrection") or 0):
            return None
        self._repair_t = time.time()
        try:
            self.api.patch_device({"iqCorrection": 0})
            self._refresh()
        except Exception as e:  # pragma: no cover
            return f"the IQ stream is stuck (constant values) and switching SDRangel's iqCorrection off failed: {e}"
        msg = ("The IQ stream was stuck at a constant full-scale value - SDRangel's IQ correction had run away. "
               "SDR Pilot switched iqCorrection off; the spectrum is real again.")
        self.board.note(msg, "warn")
        return msg

    def has_dc_spike(self) -> bool:
        n = self.receiver_note()
        return bool(n and n.get("dc_spike_at_center") is True)

    # ----------------------------------------------------------- signal level / gain advice
    def signal_level(self, seconds: float = 0.25) -> dict | None:
        """Level of the newest IQ samples: RMS and peak in dBFS and how many samples sit at full scale. Lets the AI
        judge the gain: too high clips (spurs, raised noise, broken decodes), too low wastes the 8-bit stream's
        resolution."""
        ring, fs = self.ring, max(float(self.sample_rate or 0), 1.0)
        n = int(min(ring.total, fs * seconds, ring.size // 4))
        if n < 2048:
            return None
        try:
            x = ring.read(ring.total - n, n)
        except ValueError:
            return None
        mag = np.maximum(np.abs(x.real), np.abs(x.imag))
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-12)
        clip = float(np.mean(mag >= 0.985)) * 100
        out = {"rms_dbfs": round(20 * np.log10(rms), 1), "peak_dbfs": round(20 * np.log10(float(mag.max()) + 1e-12), 1),
               "clipped_pct": round(clip, 3)}
        bits = (self.stream.meta.get("bits") if self.stream else None) or 8
        # Three cases look alike - "almost one constant value" - and need opposite fixes:
        #  pinned:    constant far from zero (SDRangel's IQ correction running away at low gain pins the samples near
        #             full scale). The spectrum is fake. Fix: iqCorrection off - NOT less gain.
        #  too quiet: sitting on the ADC's zero level and moving by 1 LSB now and then. The stream is REAL, the gain is
        #             just so low that the noise is below one 8-bit step (seen at 2.4/5 GHz with low LNA/VGA, where a
        #             quiet moment between WiFi packets is nearly constant). Fix: MORE gain.
        #  frozen:    exactly the same sample for the whole window, at a non-zero level - a stalled stream.
        lsb = 1.0 / 127.5 if bits <= 8 else 1.0 / 32768
        spread = float(np.sqrt(np.mean(np.abs(x - x.mean()) ** 2)))
        dc = abs(complex(x.mean()))
        vals, counts = np.unique(x[: min(len(x), 400000)], return_counts=True)
        mode_frac = float(counts.max()) / float(counts.sum()) if len(counts) else 0.0
        at_zero = dc < 1.6 * lsb  # the 8-bit zero code (127/128 -> about -45 dBFS) or within a step of it
        out["mode_frac"] = round(mode_frac, 4)
        dev = (self.last_summary or {}).get("device_settings") or {}
        iqc = int(dev.get("iqCorrection") or 0)
        if (dc > 0.25 and spread < 1.5 * lsb) or (not at_zero and mode_frac > 0.999):
            out["spread_dbfs"] = round(20 * np.log10(spread + 1e-12), 1)
            out["verdict"] = "stuck"
            out["pinned_dbfs"] = round(20 * np.log10(dc + 1e-12), 1)
            why = "Cause: SDRangel's IQ correction (iqCorrection=1) has run away. " if iqc else ""
            out["advice"] = (f"The IQ stream is pinned at a constant value ({20 * np.log10(dc + 1e-12):.0f} dBFS DC, no "
                             "noise): the spectrum is fake - a DC line plus quantisation steps. This is NOT overload, so "
                             "don't lower the gain. " + why + "Turn iqCorrection off (radio_device_settings("
                             "{'iqCorrection': 0})), keep dcBlock on, use normal gains (HackRF LNA 16-32, VGA 16-30) "
                             "and check signal_level again.")
            return out
        if at_zero and spread < 0.75 * lsb:
            out["spread_dbfs"] = round(20 * np.log10(spread + 1e-12), 1)
            out["verdict"] = "too_quiet"
            out["frozen_window"] = mode_frac > 0.9999
            out["advice"] = (f"The stream is real but far too quiet for its {bits}-bit resolution: {mode_frac * 100:.0f}% "
                             "of samples sit on the ADC's zero level, so the noise is below one step and anything weaker "
                             "than a strong signal is lost (the waterfall looks flat with odd lines). Raise the gain "
                             "(HackRF: VGA then LNA in steps, lnaExt +14 dB for weak signals) until signal_level says ok "
                             "- watch clipped_pct. Not a stuck stream: don't touch iqCorrection.")
            return out
        if clip > 0.05 or out["rms_dbfs"] > -8:
            out["verdict"] = "overloaded"
            out["advice"] = ("Too much gain: samples are hitting full scale, which creates spurs and false signals and "
                             "breaks decoding. Lower the gain (HackRF: switch the RF amp lnaExt off first, then reduce "
                             "LNA in 8 dB steps) until clipped_pct is ~0.")
        elif out["rms_dbfs"] < (-45 if bits <= 8 else -65):
            out["verdict"] = "low"
            out["advice"] = ("Very low level for the stream's resolution: raise the gain (HackRF: VGA, then LNA; "
                             "the RF amp lnaExt adds ~14 dB for weak signals) while watching that nothing clips.")
        else:
            out["verdict"] = "ok"
        return out

    # ----------------------------------------------------------- capture
    def capture(self, seconds: float, label: str = "") -> dict:
        self.ensure()
        self._refresh()
        fs, fc = self.sample_rate, self.center_freq
        n = int(seconds * fs)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        base = f"{ts}_{fc / 1e6:.4f}MHz_{fs / 1e6:g}Msps" + (f"_{safe}" if safe else "")
        path = os.path.join(self.cfg.data_root, "captures", base + ".cfile")
        done = threading.Event()
        cap = {"path": path, "f": open(path, "wb"), "need": n, "got": 0, "start": max(self.ring.total, self.settle_mark),
               "done": done, "seen": self.ring.total}
        with self._capture_lock:
            if self._capture is not None:
                raise RuntimeError("a capture is already running")
            # include anything already in the ring after the start mark
            self._capture = cap
        lk = self.lease.hold("IQ capture", center=fc, rate=fs)
        try:
            ok = done.wait(seconds * 3 + 5)
        finally:
            self.lease.release(lk)
        with self._capture_lock:
            self._capture = None
        cap["f"].close()
        meta = {"path": path, "format": "complex64 (GNU Radio .cfile, interleaved float32 I/Q)",
                "center_freq_hz": fc, "sample_rate": fs, "samples": cap["got"], "seconds": round(cap["got"] / fs, 4),
                "label": label, "time": ts, "device": self.hw_type,
                "device_settings": self.last_summary.get("device_settings", {}), "complete": ok}
        with open(path[:-6] + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return meta

    def _capture_feed(self, cap: dict, iq: np.ndarray) -> None:
        # stream position of iq[0]
        end_total = self.ring.total
        first = end_total - len(iq)
        if end_total <= cap["start"]:
            return
        skip = max(0, cap["start"] - first)
        x = iq[skip:]
        take = min(len(x), cap["need"] - cap["got"])
        if take > 0:
            x[:take].tofile(cap["f"])
            cap["got"] += take
        if cap["got"] >= cap["need"]:
            cap["done"].set()

    def iq_window(self, offset_hz: float = 0.0, bandwidth_hz: float = 0.0, count: int = 512,
                  trigger_db: float = 0.0, timeout_s: float = 10.0, pre_trigger: int = 64) -> dict:
        """A short, channelised slice of the live IQ (optionally waiting for a burst in that channel)."""
        self.ensure()
        self._refresh()
        fs, fc = self.sample_rate, self.center_freq
        bw = bandwidth_hz if bandwidth_hz and bandwidth_hz < fs * 0.9 else 0.0
        dec = max(1, int(fs // (4 * bw))) if bw else 1
        fs2 = fs / dec
        count = int(max(16, min(count, 4096)))
        pre = int(max(0, min(pre_trigger, count - 1)))
        ring = self.ring
        start = max(ring.total, self.settle_mark)
        trig_at = None
        floor_db = None
        if trigger_db > 0:
            nfft = 256
            freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
            band = np.abs(freqs - offset_hz) <= max(bw / 2, fs / nfft * 2)
            # noise reference: the last 0.25 s in this channel
            ref_n = int(fs * 0.25) // nfft * nfft
            if ring.total - ref_n > self.settle_mark:
                ref = ring.read(ring.total - ref_n, ref_n).reshape(-1, nfft)
                pr = np.abs(np.fft.fftshift(np.fft.fft(ref * np.hanning(nfft), axis=1), axes=1)) ** 2 / np.hanning(nfft).sum() ** 2
                floor_db = float(np.median(10 * np.log10(pr[:, band].mean(axis=1) + 1e-20)))
            end = time.time() + timeout_s
            pos = start
            blk = max(nfft * 8, int(fs * 0.01) // nfft * nfft)
            while time.time() < end and trig_at is None:
                if not ring.wait_until(pos + blk, 0.5):
                    continue
                if ring.total - pos > ring.size // 2:
                    pos = ring.total - blk
                x = ring.read(pos, blk).reshape(-1, nfft)
                pr = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(nfft), axis=1), axes=1)) ** 2 / np.hanning(nfft).sum() ** 2
                lv = 10 * np.log10(pr[:, band].mean(axis=1) + 1e-20)
                if floor_db is None:
                    floor_db = float(np.median(lv))
                hit = np.flatnonzero(lv > floor_db + trigger_db)
                if hit.size:
                    trig_at = pos + int(hit[0]) * nfft
                pos += blk
            if trig_at is None:
                raise TimeoutError(f"no burst {trigger_db} dB above the channel floor within {timeout_s}s")
            start = max(trig_at - pre * dec - 4 * dec, self.settle_mark)
        need = count * dec + 64 * dec
        if not ring.wait_until(start + need, timeout_s + 2):
            raise TimeoutError("IQ stream too slow")
        x = ring.read(start, need)
        from . import dsp
        y, fs_out = dsp.shift_filter(x, fs, offset_hz, bw or None) if (offset_hz or bw) else (x, fs)
        k = 0
        if trig_at is not None:
            k = max(0, int(round((trig_at - start) / (fs / fs_out))) - pre)
        y = y[k:k + count].astype(np.complex64)
        t0 = time.time() - (ring.total - start) / fs
        return {"samples": y, "sample_rate": fs_out, "center_freq_hz": fc + offset_hz, "start_time": t0,
                "triggered": trig_at is not None, "pre_trigger": pre if trig_at is not None else 0,
                "channel_floor_dbfs": round(floor_db, 1) if floor_db is not None else None}

    def live_psd(self, nfft: int = 1024, frames: int = 24) -> dict | None:
        x = self.latest(nfft * frames / max(self.sample_rate, 1))
        if x is None or len(x) < nfft:
            return None
        db = dsp.stft_db(x, nfft)
        avg = 10 * np.log10(np.mean(10 ** (db / 10), axis=0))
        mx = db.max(axis=0)
        return {"center": self.center_freq, "rate": self.sample_rate, "avg": np.round(avg, 1).tolist(),
                "max": np.round(mx, 1).tolist()}
