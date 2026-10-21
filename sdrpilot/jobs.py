"""Runs flowgraphs as child processes, tracks logs and output files."""
from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import grc
from .compat import child_env
from .config import Config
from .status import StatusBoard

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
log = logging.getLogger("sdrpilot.jobs")


class Job:
    def __init__(self, jid: str, name: str, script: str, folder: str, uses_live: bool, gui: bool):
        self.id, self.name, self.script, self.folder = jid, name, script, folder
        self.uses_live, self.gui = uses_live, gui
        self.log_path = os.path.join(folder, "run.log")
        self.stop_file = os.path.join(folder, ".stop")
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.status = "starting"
        self.returncode: int | None = None
        self.tail: collections.deque = collections.deque(maxlen=400)
        self.warnings: list[str] = []
        self.radio_at_start: dict = {}
        self.lock = threading.Lock()
        from .decoded import Aggregator
        self.data = Aggregator()
        self.health = ""
        self.stopped_by = ""
        self.started_by = ""  # "user" when stopped/paused from the dashboard
        self.paused_by = ""

    def outputs(self) -> list[dict]:
        out = []
        for root, _dirs, files in os.walk(self.folder):
            for f in files:
                if f in ("run.log", ".stop") or f.endswith((".pyc", ".tmp")) or f.startswith(("cmd_", "resp_")):
                    continue
                p = os.path.join(root, f)
                out.append({"file": os.path.relpath(p, self.folder).replace("\\", "/"), "path": p,
                            "bytes": os.path.getsize(p)})
        return sorted(out, key=lambda o: o["file"])

    def summary(self, tail: int = 40) -> dict:
        with self.lock:
            lines = [l for l in self.tail if not l.startswith("SPDATA ")][-tail:]
            dec = self.data.summary()
        out = {"job_id": self.id, "flowgraph": self.name, "status": self.status, "returncode": self.returncode,
               "runtime_s": round((self.ended or time.time()) - self.started, 2), "uses_live_radio": self.uses_live,
               "output_folder": self.folder, "outputs": self.outputs(), "log_tail": "\n".join(lines),
               "warnings": self.warnings}
        if dec.get("count"):
            out["decoded"] = dec
            out["decoded_file"] = os.path.join(self.folder, "decoded.jsonl")
        if self.health:
            out["flowgraph_health"] = self.health
        if self.stopped_by == "user":
            out["stopped_by"] = "the user, from the dashboard - don't restart it unless they ask"
        if getattr(self, "radio_n", 0) or getattr(self, "radio_err", 0):
            out["radio_control"] = {"applied": self.radio_n, "refused": self.radio_err,
                                    "recent": list(self.radio_log)[-5:]}
        if self.status == "paused":
            out["paused_by"] = ("the user, from the dashboard - it resumes when they press resume"
                                if self.paused_by == "user" else "you (gnuradio_job_status / pause)")
        return out


class JobManager:
    def __init__(self, cfg: Config, board: StatusBoard, radio):
        self.cfg, self.board, self.radio = cfg, board, radio
        self.jobs: dict[str, Job] = {}
        self._n = 0
        self.fg_dir = os.path.join(cfg.data_root, "flowgraphs")

    # ------------------------------------------------------------ helpers
    def flowgraph_folder(self, name: str) -> str:
        return os.path.join(self.fg_dir, grc.sanitize(name))

    def meta(self, name: str) -> dict:
        p = os.path.join(self.flowgraph_folder(name), "sdrpilot.json")
        if not os.path.exists(p):
            raise FileNotFoundError(f"No flowgraph named '{name}'. Build one with gnuradio_build_flowgraph or "
                                    f"gnuradio_write_python first. Existing: {self.list_flowgraphs()}")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def list_flowgraphs(self) -> list[str]:
        if not os.path.isdir(self.fg_dir):
            return []
        return sorted(d for d in os.listdir(self.fg_dir)
                      if os.path.exists(os.path.join(self.fg_dir, d, "sdrpilot.json")))

    def write_python(self, name: str, code: str, uses_live: bool | None = None, description: str = "") -> dict:
        name = grc.sanitize(name)
        folder = self.flowgraph_folder(name)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        if uses_live is None:
            uses_live = "sdr_source(" in code or "SDRPILOT_ZMQ" in code
        try:
            compile(code, path, "exec")
            syntax = None
        except SyntaxError as e:
            syntax = f"line {e.lineno}: {e.msg}"
        import hashlib
        meta = {"name": name, "kind": "python", "py": path, "uses_live_radio": uses_live, "gui": False,
                "description": description, "built": time.time(), "valid": syntax is None,
                "spec_digest": "py:" + hashlib.sha1(code.encode()).hexdigest()[:16]}
        from . import fglib
        fglib.carry_health(fglib.read(self.fg_dir, name), meta)
        with open(os.path.join(folder, "sdrpilot.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return {"name": name, "python_file": path, "uses_live_radio": uses_live, "syntax_error": syntax}

    # ------------------------------------------------------------ run / stop
    def run(self, name: str, duration_s: float | None = None, wait_s: float = 8.0, by: str = "") -> dict:
        meta = self.meta(name)
        warnings = []
        if meta.get("kind") != "python" and meta.get("uses_live_radio"):
            self.radio.ensure()
            st = self.radio.status()
            explicit = set(meta.get("explicit_vars") or [])
            stale = []
            if "samp_rate" not in explicit and float(meta.get("built_samp_rate") or 0) != float(st["sample_rate"]):
                stale.append("samp_rate")
            if "center_freq" not in explicit and float(meta.get("built_center_freq") or 0) != float(st["center_freq_hz"]):
                stale.append("center_freq")
            if stale:
                r = grc.build_flowgraph(meta["spec"], self.fg_dir, st, self.cfg.zmq_address)
                if not r["valid"]:
                    raise RuntimeError(f"rebuild for new radio settings failed: {r['errors']}")
                meta = self.meta(name)
                warnings.append(f"rebuilt with current radio {', '.join(stale)} "
                                f"({st['center_freq_hz'] / 1e6:.4f} MHz, {st['sample_rate'] / 1e6:g} MS/s)")
        if (meta.get("kind") == "grc" and not meta.get("hier") and not meta.get("draft") and meta.get("spec")
                and int(meta.get("builder_rev") or 1) < grc.BUILDER_REV):
            st = self.radio.status() if meta.get("uses_live_radio") else None
            r = grc.build_flowgraph(meta["spec"], self.fg_dir, st, self.cfg.zmq_address, history=meta.get("history"))
            if r["valid"]:
                meta = self.meta(name)
                warnings.append("rebuilt with SDR Pilot's current flowgraph builder (it was built by an older "
                                "version)" + (": " + "; ".join(r["notes"]) if r.get("notes") else ""))
        if meta.get("hier"):
            raise RuntimeError(f"'{name}' is a reusable block, not a flowgraph: use it as a block type inside a "
                               f"flowgraph (gnuradio_build_flowgraph blocks=[{{'name': 'x', 'type': '{name}'}}])")
        if meta.get("draft") and meta.get("kind") != "python":
            ins = [p for p in meta.get("open_ports") or [] if "(input)" in p]
            if ins:
                raise RuntimeError(f"flowgraph '{name}' is still being built - nothing feeds input(s) {ins[:8]}. "
                                   "Connect them (gnuradio_add_block connect_from / gnuradio_edit_flowgraph "
                                   "add_connections ['src:0 -> blk:0']) first")
            # only outputs left open (e.g. audio not wanted): finish the draft - they get null sinks
            st = self.radio.status() if meta.get("uses_live_radio") else None
            r = grc.build_flowgraph(meta["spec"], self.fg_dir, st, self.cfg.zmq_address,
                                    history=meta.get("history"))
            if not r["valid"]:
                raise RuntimeError(f"flowgraph '{name}' isn't finished: {r['errors'][:6]}")
            meta = self.meta(name)
            if r.get("notes"):
                warnings += [n for n in r["notes"] if "null sink" in n]
        if not meta.get("valid", True) or not meta.get("py"):
            raise RuntimeError(f"flowgraph '{name}' is not valid - fix the errors reported by the build first")
        lease = None
        if meta.get("uses_live_radio"):
            self.radio.ensure()
            self.radio.publisher.acquire()
            lease = self.radio.lease.hold(f"live flowgraph '{meta['name']}'", center=self.radio.center_freq,
                                          rate=self.radio.sample_rate)
        self._n += 1
        jid = f"j{self._n}"
        folder = os.path.join(self.cfg.data_root, "jobs",
                              f"{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}_{jid}_{meta['name']}")
        os.makedirs(folder, exist_ok=True)
        job = Job(jid, meta["name"], meta["py"], folder, bool(meta.get("uses_live_radio")), bool(meta.get("gui")))
        job.warnings = warnings
        job.lease = lease
        job.started_by = by
        job.radio_at_start = {"center": self.radio.center_freq, "rate": self.radio.sample_rate, "epoch": self.radio.epoch}
        env = child_env({"SDRPILOT_ZMQ": self.radio.publisher.address, "SDRPILOT_SAMP_RATE": self.radio.sample_rate or 0,
                         "SDRPILOT_CENTER_FREQ": self.radio.center_freq or 0, "SDRPILOT_JOB_DIR": folder})
        if job.gui:
            cmd = [sys.executable, "-u", meta["py"]]
        else:
            cmd = [sys.executable, "-u", "-m", "sdrpilot.runner", meta["py"], "--stop-file", job.stop_file]
            if duration_s:
                cmd += ["--duration", str(duration_s)]
        job.proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        job.status = "running"
        self.jobs[jid] = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        threading.Thread(target=self._watch, args=(job, duration_s), daemon=True).start()
        self.board.note(f"Started flowgraph '{job.name}' as job {jid}", "event")
        # wait a little so the first result already says whether it started cleanly
        end = time.time() + max(0.0, wait_s)
        while time.time() < end and job.proc.poll() is None:
            time.sleep(0.1)
        time.sleep(0.2)
        return job.summary()

    def _pump(self, job: Job) -> None:
        from .decoded import parse
        dec_f = None
        first = True
        with open(job.log_path, "w", encoding="utf-8", errors="replace") as log:
            for raw in iter(job.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if "vmcircbuf_prefs" in line or line.startswith("Using avx") or line.startswith("CPU Features"):
                    continue
                log.write(line + "\n")
                log.flush()
                if line.startswith("SPRADIO "):
                    self._radio_request(job, line[8:])
                    continue
                rec = parse(line) if "SPDATA " in line else None
                with job.lock:
                    if rec is not None:
                        job.data.add(rec)
                    else:
                        job.tail.append(line)
                if rec is not None:
                    try:
                        if dec_f is None:
                            dec_f = open(os.path.join(job.folder, "decoded.jsonl"), "a", encoding="utf-8")
                        dec_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                        dec_f.flush()
                    except OSError:
                        pass
                    if first:
                        first = False
                        self.board.note(f"Job {job.id} ({job.name}) is decoding data - see the Decoded view", "event")
                        try:
                            from . import fglib
                            fglib.update(self.fg_dir, job.name, health="works", worked_ever=True)
                        except Exception:
                            pass
        if dec_f is not None:
            dec_f.close()

    # ---- flowgraphs that control the radio (sp_radio_* blocks print "SPRADIO {json}")
    radio_ctl = None  # set by the server: callable(job, request) -> dict (what was applied)

    def _radio_request(self, job: Job, text: str) -> None:
        try:
            req = json.loads(text)
        except ValueError:
            return
        with job.lock:
            if not hasattr(job, "radio_q"):
                job.radio_q, job.radio_log = {}, collections.deque(maxlen=30)
                job.radio_n, job.radio_err = 0, 0
                threading.Thread(target=self._radio_worker, args=(job,), daemon=True).start()
            # coalesce: only the newest request per (op, parameters) waits - a fast hopper can't build a queue
            key = (req.get("op"), tuple(sorted(list((req.get("set") or {}).keys()) +
                                               list((req.get("expr") or {}).keys()))))
            if req.get("op") == "add" and key in job.radio_q:  # relative steps add up
                old = job.radio_q[key]
                for k, v in (req.get("set") or {}).items():
                    try:
                        req["set"][k] = float(v) + float((old.get("set") or {}).get(k, 0))
                    except (TypeError, ValueError):
                        pass
            job.radio_q[key] = req

    def _radio_worker(self, job: Job) -> None:
        last_note = 0.0
        while job.proc is not None and (job.proc.poll() is None or job.radio_q):
            with job.lock:
                items = list(job.radio_q.values())
                job.radio_q.clear()
            if not items:
                time.sleep(0.05)
                continue
            for req in items:
                if job.status == "paused" or self.radio_ctl is None:
                    continue
                try:
                    res = self.radio_ctl(job, req)
                    job.radio_n += 1
                    job.radio_log.append({"t": round(time.time(), 2), "applied": res})
                    for param, var in (req.get("update_var") or {}).items():
                        if param in (res or {}) and not job.gui:
                            try:
                                self.command(job.id, {"op": "set", "var": var, "value": res[param]}, timeout=3)
                            except Exception as e:
                                job.radio_log.append({"t": round(time.time(), 2), "error": f"update {var}: {e}"})
                    if time.time() - last_note > 5:
                        last_note = time.time()
                        self.board.note(f"Flowgraph {job.name} set the radio: "
                                        + ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
                                                    for k, v in (res or {}).items()), "event")
                except Exception as e:
                    job.radio_err += 1
                    job.radio_log.append({"t": round(time.time(), 2), "error": str(e)[:300], "request": req})
                    if job.radio_err <= 3:
                        job.warnings.append(f"radio request from the flowgraph refused: {str(e)[:200]}")
                        self.board.note(f"Flowgraph {job.name}: radio request refused - {str(e)[:160]}", "warn")

    def _watch(self, job: Job, duration_s: float | None) -> None:
        rc = job.proc.wait()
        job.returncode = rc
        job.ended = time.time()
        if job.status in ("stopping", "paused"):
            job.status = "stopped"
        else:
            job.status = "finished" if rc == 0 else "failed"
        if any("Traceback" in l for l in list(job.tail)) and job.status == "finished":
            job.status = "finished_with_errors"
        if job.uses_live:
            self.radio.publisher.release()
            if getattr(job, "lease", None):
                self.radio.lease.release(job.lease)
            if self.radio.epoch != job.radio_at_start.get("epoch"):
                job.warnings.append("the radio was retuned while this job ran - results after the retune used the new frequency")
        try:
            from . import fglib
            err = ""
            if job.status in ("failed", "finished_with_errors"):
                tail = [l for l in list(job.tail) if l.strip()]
                err = next((l for l in reversed(tail) if "Error" in l or "error" in l), tail[-1] if tail else "")
            h = fglib.record_run(self.fg_dir, job.name, job.status, job.data.count, job.outputs(),
                                 (job.ended or time.time()) - job.started, job.id, err)
            job.health = h
        except Exception:  # pragma: no cover - never let bookkeeping break a job
            log.exception("recording the run of %s failed", job.name)
        self.board.note(f"Job {job.id} ({job.name}) {job.status}", "event")
        if job.status in ("failed", "finished_with_errors"):
            log.warning("job %s (%s) %s rc=%s, log %s:\n%s", job.id, job.name, job.status, rc, job.log_path,
                        "\n".join(list(job.tail)[-40:]))
        else:
            log.info("job %s (%s) %s rc=%s", job.id, job.name, job.status, rc)

    def stop(self, jid: str, timeout: float = 6.0, by: str = "") -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(f"no job {jid}")
        if job.proc and job.proc.poll() is None:
            job.status = "stopping"
            if by:
                job.stopped_by = by
            open(job.stop_file, "w").close()
            try:
                job.proc.wait(timeout=0.5 if job.gui else timeout)
            except subprocess.TimeoutExpired:
                job.proc.terminate()
                try:
                    job.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    job.proc.kill()
        time.sleep(0.2)
        return job.summary()

    def pause(self, jid: str, by: str = "") -> dict:
        job = self.get(jid)
        if job.gui:
            raise RuntimeError("Qt GUI flowgraphs can't be paused (they run outside the runner) - stop it instead")
        if job.status == "paused":
            return job.summary(tail=8)
        r = self.command(jid, {"op": "pause"}, timeout=8.0)
        job.status, job.paused_by = "paused", by
        self.board.note(f"Job {jid} ({job.name}) paused" + (" by the user" if by == "user" else ""), "event")
        out = job.summary(tail=8)
        out["state"] = r.get("state")
        return out

    def resume(self, jid: str) -> dict:
        job = self.get(jid)
        if job.status != "paused":
            raise RuntimeError(f"job {jid} isn't paused ({job.status})")
        self.command(jid, {"op": "resume"}, timeout=8.0)
        job.status, job.paused_by = "running", ""
        self.board.note(f"Job {jid} ({job.name}) resumed", "event")
        return job.summary(tail=8)

    def rerun(self, jid: str) -> dict:
        """Start the same flowgraph again as a new job (after it stopped / finished / failed)."""
        job = self.get(jid)
        if job.proc and job.proc.poll() is None:
            raise RuntimeError(f"job {jid} is still running - stop it first")
        return self.run(job.name, wait_s=3.0, by="user")

    def command(self, jid: str, cmd: dict, timeout: float = 5.0) -> dict:
        """Send a live-control command to a running flowgraph (handled by sdrpilot.runner)."""
        job = self.get(jid)
        if job.gui:
            raise RuntimeError("live control isn't available for Qt GUI flowgraphs (they run outside the runner)")
        if not job.proc or job.proc.poll() is not None:
            raise RuntimeError(f"job {jid} is not running ({job.status})")
        self._cmd_n = getattr(self, "_cmd_n", 0) + 1
        rid = f"{os.getpid()}_{self._cmd_n}"
        tmp = os.path.join(job.folder, f"cmd_{rid}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cmd, f)
        os.replace(tmp, os.path.join(job.folder, f"cmd_{rid}.json"))
        resp_path = os.path.join(job.folder, f"resp_{rid}.json")
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(resp_path):
                with open(resp_path, encoding="utf-8") as f:
                    resp = json.load(f)
                os.remove(resp_path)
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error"))
                return resp
            time.sleep(0.05)
        raise TimeoutError(f"job {jid} did not answer within {timeout}s")

    def get(self, jid: str) -> Job:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(f"no job {jid}. Jobs: {list(self.jobs)}")
        return job

    def stop_all(self) -> None:
        for jid, j in list(self.jobs.items()):
            if j.proc and j.proc.poll() is None:
                try:
                    self.stop(jid, timeout=3)
                except Exception:
                    pass

    def overview(self) -> list[dict]:
        out = []
        for j in list(self.jobs.values())[-30:]:
            s = j.summary(tail=8)
            s["outputs"] = [{"file": o["file"], "bytes": o["bytes"]} for o in s["outputs"]][:20]
            s.pop("decoded", None)
            s["decoded_count"] = j.data.count
            out.append(s)
        return out

    def decoded_view(self, jid: str = "") -> dict:
        """The Decoded view: the chosen job's merged data, plus which jobs have data."""
        with_data = [j for j in self.jobs.values() if j.data.count]
        avail = [{"job_id": j.id, "flowgraph": j.name, "status": j.status, "count": j.data.count,
                  "kinds": dict(j.data.kinds)} for j in with_data[-20:]]
        job = self.jobs.get(jid) if jid else None
        if job is None and with_data:
            running = [j for j in with_data if j.status == "running"]
            job = (running or with_data)[-1]
        out = {"jobs": avail, "job": None}
        if job is not None:
            with job.lock:
                out["job"] = {"job_id": job.id, "flowgraph": job.name, "status": job.status, **job.data.view(),
                              "recent": list(job.data.recent)[-60:]}
        return out

    def open_in_companion(self, name: str) -> str:
        meta = self.meta(name)
        path = meta.get("grc")
        if not path:
            raise ValueError(f"'{name}' is a hand-written Python flowgraph; there is no .grc to open")
        code = "import sys; sys.argv=['gnuradio-companion', sys.argv[1]]; from gnuradio.grc.main import main; sys.exit(main())"
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-c", code, path], env=child_env(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        return path
