"""Helpers for hand-written Python flowgraphs run by SDR Pilot.

    from sdrpilot.fg import sdr_source, capture_source, radio

    class top(gr.top_block):
        def __init__(self):
            gr.top_block.__init__(self)
            r = radio()                      # {'samp_rate', 'center_freq', 'zmq'}
            src = sdr_source()               # live complex IQ from SDRangel
            # or: src, meta = capture_source("path/to/capture.cfile")
"""
from __future__ import annotations

import json
import os


def radio() -> dict:
    return {"samp_rate": float(os.environ.get("SDRPILOT_SAMP_RATE", "0") or 0),
            "center_freq": float(os.environ.get("SDRPILOT_CENTER_FREQ", "0") or 0),
            "zmq": os.environ.get("SDRPILOT_ZMQ", "tcp://127.0.0.1:5600")}


def sdr_source():
    from gnuradio import gr, zeromq
    return zeromq.sub_source(gr.sizeof_gr_complex, 1, radio()["zmq"], 100, False, -1, "", False)


def capture_source(path: str, repeat: bool = False):
    from gnuradio import blocks, gr
    meta = {}
    side = os.path.splitext(path)[0] + ".json"
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            meta = json.load(f)
    return blocks.file_source(gr.sizeof_gr_complex, path, repeat), meta


def out_path(name: str) -> str:
    """Path inside this job's output folder (the working directory)."""
    return os.path.join(os.environ.get("SDRPILOT_JOB_DIR", os.getcwd()), name)
