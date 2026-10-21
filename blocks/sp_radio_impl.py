"""SDR Pilot radio-control blocks (installed into GNU Radio's hier-block folder): let a running flowgraph change
the radio itself - follow a frequency hopper, keep a fading signal at a good level with its own AGC, switch
antenna/bias-tee/filters/any device setting.

The blocks print one JSON line per request ("SPRADIO {...}") on stdout; SDR Pilot, which runs the flowgraph, reads
it from the job's output and applies it to the SDR the same way its own radio tools do (center_freq, sample_rate,
gain, agc, or any SDRangel device setting by name). Values may be numbers or expressions: `x` is the value that
arrived, plus every number SDR Pilot knows (center_freq, sample_rate, gain, saved analysis variables...), e.g.
'x * 1e6', 'center_freq + x', '2402e6 + 2e6 * x'. Requests are rate-limited by min_interval."""
import json
import math
import sys
import time

import numpy as np
import pmt
from gnuradio import gr

PREFIX = "SPRADIO "


def _send(block, sets: dict, exprs: dict | None = None, op: str = "set", lo=None, hi=None, update_var=None,
          x=None):
    req = {"block": block, "op": op, "set": sets, "t": round(time.time(), 3)}
    if exprs:
        req["expr"] = exprs
    if x is not None:
        req["x"] = x
    if lo is not None and str(lo) != "":
        req["min"] = lo
    if hi is not None and str(hi) != "":
        req["max"] = hi
    if update_var:  # {radio parameter: flowgraph variable to set to the applied value}
        req["update_var"] = update_var
    try:
        sys.stdout.write(PREFIX + json.dumps(req, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


class radio_control(gr.basic_block):
    """Message in 'cmd' -> radio. A number sets `param` to the value of `value` (an expression of x); a dict
    {"center_freq": 2.426e9, "gain": 30} or a pair (gain . 30) or a string 'gain=30' sets those parameters directly.
    Any parameter name SDR Pilot's radio tools accept works: center_freq, sample_rate, gain, agc, or an SDRangel device
    setting (lnaGain, vgaGain, biasT, antennaPath, bandwidth, ...)."""

    def __init__(self, param="center_freq", value="x", min_interval=0.25, minimum="", maximum="", update_var=""):
        gr.basic_block.__init__(self, name="sp_radio_control", in_sig=None, out_sig=None)
        self.param, self.value = str(param or ""), str(value or "x")
        self.min_interval = float(min_interval or 0)
        self.lo, self.hi, self.update_var = minimum, maximum, str(update_var or "")
        self._last = 0.0
        self._pending = None
        self.message_port_register_in(pmt.intern("cmd"))
        self.set_msg_handler(pmt.intern("cmd"), self.handle)

    def handle(self, msg):
        try:
            v = pmt.to_python(msg)
        except Exception:
            v = pmt.symbol_to_string(msg) if pmt.is_symbol(msg) else None
        sets, exprs, x = {}, {}, None
        if isinstance(v, dict):
            sets = {str(k): (_num(x_) if _num(x_) is not None else str(x_)) for k, x_ in v.items()}
        elif isinstance(v, (tuple, list)) and len(v) == 2 and isinstance(v[0], str):
            sets = {v[0]: _num(v[1]) if _num(v[1]) is not None else str(v[1])}
        elif isinstance(v, (tuple, list)) and len(v) == 2 and isinstance(v[0], dict):  # PDU: use its metadata
            meta = v[0]
            if self.param in meta:
                x = _num(meta[self.param])
        elif isinstance(v, str) and "=" in v:
            k, _, val = v.partition("=")
            sets = {k.strip(): _num(val) if _num(val) is not None else val.strip()}
        else:
            if isinstance(v, (list, tuple, np.ndarray)) and len(v):
                v = v[0]
            x = _num(v)
        if x is not None:
            if not self.param:
                return
            if self.value.strip() in ("", "x"):
                sets = {self.param: x}
            else:
                exprs = {self.param: self.value}
        if not sets and not exprs:
            return
        now = time.time()
        if now - self._last < self.min_interval:
            return  # rate limit (the newest request after the gap wins)
        self._last = now
        _send(self.name(), sets, exprs, "set", self.lo, self.hi,
              {self.param: self.update_var} if self.update_var and self.param else None, x)


class radio_param_from_stream(gr.sync_block):
    """Float stream in -> one radio parameter. Every `interval` seconds the stream's mean/max/min/last over that
    time becomes x, and `param` is set to `value` (an expression of x). With deadband > 0 it only sends when x moved
    by more than that."""

    def __init__(self, param="gain", value="x", interval=1.0, reduce="mean", deadband=0.0, minimum="", maximum="",
                 update_var=""):
        gr.sync_block.__init__(self, name="sp_radio_param_from_stream", in_sig=[np.float32], out_sig=None)
        self.param, self.value = str(param), str(value or "x")
        self.interval, self.reduce, self.deadband = float(interval or 1.0), str(reduce or "mean"), float(deadband or 0)
        self.lo, self.hi, self.update_var = minimum, maximum, str(update_var or "")
        self._acc, self._n, self._mx, self._mn, self._lastv = 0.0, 0, -np.inf, np.inf, None
        self._t0, self._sent = time.time(), None

    def work(self, input_items, output_items):
        a = input_items[0]
        a = a[np.isfinite(a)]
        if len(a):
            self._acc += float(np.sum(a))
            self._n += len(a)
            self._mx, self._mn, self._lastv = max(self._mx, float(a.max())), min(self._mn, float(a.min())), float(a[-1])
        if time.time() - self._t0 >= self.interval and self._n:
            x = {"mean": self._acc / self._n, "max": self._mx, "min": self._mn, "last": self._lastv}.get(
                self.reduce, self._acc / self._n)
            if self._sent is None or abs(x - self._sent) > self.deadband:
                self._sent = x
                uv = {self.param: self.update_var} if self.update_var else None
                if self.value.strip() in ("", "x"):
                    _send(self.name(), {self.param: x}, None, "set", self.lo, self.hi, uv, x)
                else:
                    _send(self.name(), {}, {self.param: self.value}, "set", self.lo, self.hi, uv, x)
            self._acc, self._n, self._mx, self._mn, self._t0 = 0.0, 0, -np.inf, np.inf, time.time()
        return len(input_items[0])


class radio_agc(gr.sync_block):
    """Closed-loop gain control for a signal that fades: the input is the signal's level or SNR in dB (e.g. from a
    power estimate in dB). Every `interval` seconds, if the average is outside target_db +/- deadband_db, it asks for
    gain += kp * (target_db - level), at most max_step dB at a time, kept within min_gain..max_gain. param: which gain
    ('gain' = SDR Pilot's overall gain, or a stage such as 'lnaGain' / 'vgaGain')."""

    def __init__(self, target_db=20.0, param="gain", kp=0.5, deadband_db=3.0, max_step=10.0, min_gain=0.0,
                 max_gain=60.0, interval=1.0, settle=1.5):
        gr.sync_block.__init__(self, name="sp_radio_agc", in_sig=[np.float32], out_sig=None)
        self.target, self.param, self.kp = float(target_db), str(param or "gain"), float(kp)
        self.dead, self.max_step = float(deadband_db), float(max_step)
        self.lo, self.hi, self.interval, self.settle = float(min_gain), float(max_gain), float(interval), float(settle)
        self._acc, self._n, self._t0, self._quiet_until = 0.0, 0, time.time(), 0.0

    def set_target_db(self, v):
        self.target = float(v)

    def work(self, input_items, output_items):
        a = input_items[0]
        now = time.time()
        if now >= self._quiet_until:  # ignore samples while the last change settles
            a = a[np.isfinite(a)]
            self._acc += float(np.sum(a))
            self._n += len(a)
        if now - self._t0 >= self.interval and self._n:
            level = self._acc / self._n
            err = self.target - level
            if abs(err) > self.dead:
                step = max(-self.max_step, min(self.max_step, self.kp * err))
                _send(self.name(), {self.param: round(step, 2)}, None, "add", self.lo, self.hi, None, round(level, 2))
                self._quiet_until = now + self.settle
            self._acc, self._n, self._t0 = 0.0, 0, now
        return len(input_items[0])
