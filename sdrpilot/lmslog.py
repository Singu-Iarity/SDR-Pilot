"""Exact prompt-processing progress for LM Studio.

LM Studio runs llama.cpp as its engine, but its own HTTP server doesn't pass llama.cpp's progress through the
OpenAI-compatible endpoint the dashboard agent uses. Its server log does show it ("Prompt processing progress: 15.5%",
and llama.cpp's "prompt eval time = ... ms / N tokens" timings), and LM Studio's CLI streams that log:
`lms log stream --source server` (LM Studio 0.3.26+). This reads it in the background.

Best effort: if `lms` isn't found or the stream ends, the agent falls back to its own estimate."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger("sdrpilot.lmslog")

PROGRESS_RE = re.compile(r"(?:\[([^\]\s]+)\])?\s*Prompt processing progress:\s*([\d.]+)\s*%", re.I)
PROMPT_EVAL_RE = re.compile(r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", re.I)
GEN_EVAL_RE = re.compile(r"(?<!prompt )\beval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", re.I)
RETRY_S = 60.0


def find_lms() -> str | None:
    p = shutil.which("lms")
    if p:
        return p
    home = os.path.expanduser("~")
    for c in (os.path.join(home, ".lmstudio", "bin", "lms.exe"), os.path.join(home, ".lmstudio", "bin", "lms"),
              os.path.join(home, ".cache", "lm-studio", "bin", "lms")):
        if os.path.isfile(c):
            return c
    return None


class LMStudioLogTap:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.progress: tuple[float, float, str] | None = None   # (time, fraction 0..1, model)
        self.prompt_eval: tuple[float, float, int] | None = None  # (time, ms, tokens)
        self.gen_eval: tuple[float, float, int] | None = None
        self.lines = 0
        self._failed_at = 0.0
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> bool:
        """Start `lms log stream --source server` if it isn't running (retries at most once a minute)."""
        with self._lock:
            if self.running:
                return True
            if time.time() - self._failed_at < RETRY_S:
                return False
            exe = find_lms()
            if not exe:
                self._failed_at = time.time()
                return False
            kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                        "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            try:
                self.proc = subprocess.Popen([exe, "log", "stream", "--source", "server"], **kw)
            except OSError as e:
                log.info("lms log stream unavailable: %s", e)
                self._failed_at = time.time()
                return False
            threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="sdrpilot-lmslog").start()
            log.info("reading LM Studio's server log for prompt progress (%s)", exe)
            return True

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self.feed(line)
        except Exception:  # pragma: no cover
            pass
        if proc.poll() is None:
            proc.kill()
        self._failed_at = time.time()

    def feed(self, line: str) -> None:
        self.lines += 1
        now = time.time()
        m = PROGRESS_RE.search(line)
        if m:
            self.progress = (now, max(0.0, min(1.0, float(m.group(2)) / 100.0)), m.group(1) or "")
            return
        m = PROMPT_EVAL_RE.search(line)
        if m:
            self.prompt_eval = (now, float(m.group(1)), int(m.group(2)))
            return
        m = GEN_EVAL_RE.search(line)
        if m:
            self.gen_eval = (now, float(m.group(1)), int(m.group(2)))

    def progress_since(self, t0: float, model: str = "") -> float | None:
        p = self.progress
        if not p or p[0] < t0:
            return None
        if model and p[2] and p[2] != model and not model.endswith(p[2]) and not p[2].endswith(model):
            return None  # another model loaded in LM Studio
        return p[1]

    def stop(self) -> None:
        if self.running:
            try:
                self.proc.kill()  # type: ignore[union-attr]
            except Exception:
                pass
