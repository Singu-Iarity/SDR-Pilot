"""Shared status board: what the AI is doing, task progress, activity log.

Everything the dashboard shows comes from here. All methods are thread-safe.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from typing import Any

STEP_STATES = ("pending", "active", "done", "failed", "skipped", "unreported")


class StatusBoard:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self.started = time.time()
        self.task: dict[str, Any] = {"goal": "", "steps": [], "started": None, "finished": None, "outcome": ""}
        self.activity: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []  # latest analysis images (newest last)
        self.radio: dict[str, Any] = {"state": "idle"}
        self.version = 0
        self.last_client_activity = 0.0
        # dashboard chat: messages typed on the dashboard and replies (from Claude or the dashboard agent)
        self.chat: list[dict[str, Any]] = []
        self.chat_details: dict[int, dict] = {}
        self.user_actions: list[dict] = []  # things the AI asked the user to do (antenna, placement...)
        self._ua_ids = itertools.count(1)  # full tool call (arguments + result) per chat message, on demand
        self._chat_ids = itertools.count(1)
        self._chat_rev = 0
        self.chat_state: dict[str, Any] = {"listening_until": 0.0, "agent": None}

    # ----- task / progress -------------------------------------------------
    # Steps can have sub-steps ("substeps", same shape, nested). A step is addressed by a path: 2 or "2" (third
    # step), "2.0" (its first sub-step), "2.0.1"... (0-based everywhere).
    history_path: str | None = None  # set by the server: finished/replaced tasks are kept there

    @staticmethod
    def _mk_steps(steps) -> list[dict]:
        out = []
        for st in steps or []:
            if isinstance(st, dict):
                title = str(next((st[k] for k in ("title", "name", "step", "text", "label", "goal", "description",
                                                  "note", "task") if st.get(k)), "") or "")
                kids = st.get("steps") or st.get("substeps") or st.get("sub") or []
            else:
                title, kids = str(st), []
            d = {"title": title, "status": "pending", "note": ""}
            if kids:
                d["substeps"] = StatusBoard._mk_steps(kids)
            out.append(d)
        return out

    def _path(self, index) -> list[int]:
        if isinstance(index, (int, float)) and not isinstance(index, bool):
            return [int(index)]
        parts = [p for p in re.split(r"[.,/ ]+", str(index).strip()) if p != ""]
        if not parts or not all(p.lstrip("-").isdigit() for p in parts):
            raise ValueError(f"step index '{index}': use a number (2) or a path to a sub-step ('2.1')")
        return [int(p) for p in parts]

    def _locate(self, index, allow_end: bool = False) -> tuple[list, int, dict | None]:
        """-> (the list the step lives in, its position, the parent step or None)."""
        path = self._path(index)
        lst, parent = self.task["steps"], None
        for depth, i in enumerate(path):
            last = depth == len(path) - 1
            hi = len(lst) if (allow_end and last) else len(lst) - 1
            if not 0 <= i <= hi:
                where = "steps" if parent is None else f"sub-steps of '{parent['title']}'"
                raise ValueError(f"step index {index}: {i} is out of range for the {len(lst)} {where} (0..{len(lst) - 1})")
            if last:
                return lst, i, parent
            parent = lst[i]
            lst = parent.setdefault("substeps", [])
        raise ValueError(f"bad step index {index}")  # pragma: no cover

    def _archive(self, how: str) -> None:
        """Keep the current task (if any) in the history file: what was being done, how far it got."""
        t = self.task
        if not t.get("goal") or not self.history_path or t.get("_archived"):
            return
        notes = [{"t": e["t"], "text": e["title"]} for e in self.activity
                 if e["kind"] in ("note", "event") and t.get("started") and e["t"] >= t["started"]][-40:]
        rec = {"id": f"{int(t.get('started') or time.time())}-{os.getpid()}", "goal": t["goal"],
               "steps": t["steps"], "started": t.get("started"), "finished": t.get("finished") or time.time(),
               "outcome": t.get("outcome", ""), "success": t.get("success"), "how": how,
               "progress": round(self.progress(), 3), "pid": os.getpid(), "who": self.chat_state.get("who") or "",
               "notes": notes}
        try:
            os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            t["_archived"] = True
        except OSError:  # pragma: no cover
            pass

    def set_task(self, goal: str, steps: list) -> dict:
        with self._lock:
            if self.task.get("goal"):
                self._archive("finished" if self.task.get("finished") else "replaced by a new task")
            self.task = {
                "goal": goal,
                "steps": self._mk_steps(steps),
                "started": time.time(),
                "finished": None,
                "outcome": "",
            }
            if self.task["steps"]:
                self._activate_first(self.task["steps"][0])
            self._bump()
            return self.task

    @staticmethod
    def _activate_first(step: dict) -> None:
        step["status"] = "active"
        if step.get("substeps"):
            StatusBoard._activate_first(step["substeps"][0])

    def _all_steps(self, lst=None):
        for st in (self.task["steps"] if lst is None else lst):
            yield st
            yield from self._all_steps(st.get("substeps") or [])

    def update_step(self, index, status: str, note: str = "") -> list:
        with self._lock:
            lst, i, parent = self._locate(index)
            if status not in STEP_STATES:
                raise ValueError(f"status must be one of {STEP_STATES}")
            if status == "active":  # one current step per list (and its parents become active too)
                for j, st in enumerate(lst):
                    if j != i and st["status"] == "active":
                        st["status"] = "pending"
                path = self._path(index)
                anc_lst = self.task["steps"]
                for depth, k in enumerate(path[:-1]):
                    for j, st in enumerate(anc_lst):
                        if j != k and st["status"] == "active":
                            st["status"] = "pending"
                    anc_lst[k]["status"] = "active"
                    anc_lst = anc_lst[k]["substeps"]
            lst[i]["status"] = status
            if note:
                lst[i]["note"] = note
            if status in ("pending", "active"):
                self._reopen()
            passed = []
            path = self._path(index)
            if len(path) > 1 and status in ("active", "done", "skipped", "failed"):
                # working inside step 2 means step 2 is the current one: its parents become active and earlier
                # steps that were never closed are flagged as unreported
                anc = self.task["steps"]
                for k in path[:-1]:
                    for j, st in enumerate(anc[:k]):
                        if st["status"] in ("pending", "active"):
                            st["status"] = "unreported"
                            passed.append(j)
                    for st in anc[k + 1:]:
                        if st["status"] == "active":
                            st["status"] = "pending"
                    if anc[k]["status"] in ("pending", "done", "unreported", "skipped"):
                        anc[k]["status"] = "active"
                    anc = anc[k].setdefault("substeps", [])
            if status in ("done", "skipped", "failed"):
                for j, st in enumerate(lst[:i]):
                    if st["status"] in ("pending", "active"):
                        st["status"] = "unreported"
                        passed.append(j)
                for st in self._all_steps(lst[i].get("substeps") or []):  # finishing a step finishes its sub-steps
                    if st["status"] in ("pending", "active"):
                        st["status"] = "unreported" if status == "done" else "skipped"
            if status in ("done", "skipped") and not any(st["status"] == "active" for st in lst):
                for st in lst[i + 1:]:
                    if st["status"] == "pending":
                        self._activate_first(st)
                        break
                else:  # the last sub-step finished: so did its parent step
                    if parent is not None and all(st["status"] in ("done", "skipped", "unreported") for st in lst):
                        self._bump()
                        return passed + self.update_step(".".join(str(p) for p in path[:-1]), "done")
            self._bump()
            return passed

    def add_step(self, title: str) -> dict:
        with self._lock:
            self.task["steps"].append({"title": title, "status": "pending", "note": ""})
            self._bump()
            return self.task

    def _reopen(self) -> None:
        """A finished task gets more work (a step moved back, a step added): show it as in progress again."""
        if self.task.get("finished"):
            self.task["finished"] = None
            self.task["outcome"] = ""
            self.task.pop("success", None)
            self.task.pop("_archived", None)

    def insert_step(self, index, title: str, status: str = "pending", note: str = "", substeps=None) -> None:
        with self._lock:
            lst, i, _parent = self._locate(index, allow_end=True)
            if status not in STEP_STATES:
                raise ValueError(f"status must be one of {STEP_STATES}")
            d = {"title": title, "status": status, "note": note}
            if substeps:
                d["substeps"] = self._mk_steps(substeps)
            lst.insert(i, d)
            if status in ("pending", "active"):
                self._reopen()
            if status == "active":
                self.update_step(".".join(str(p) for p in self._path(index)), "active")
            elif status == "pending" and _parent is not None and _parent["status"] == "done":
                _parent["status"] = "active" if any(x["status"] == "active" for x in lst) else "pending"
            self._bump()

    def delete_step(self, index) -> dict:
        with self._lock:
            lst, i, _parent = self._locate(index)
            gone = lst.pop(i)
            if gone["status"] == "active" and not any(st["status"] == "active" for st in lst):
                for st in lst[i:]:
                    if st["status"] == "pending":
                        st["status"] = "active"
                        break
            self._bump()
            return gone

    def move_step(self, index, to) -> None:
        with self._lock:
            lst, i, _parent = self._locate(index)
            st = lst.pop(i)
            to_i = self._path(to)[-1]
            lst.insert(max(0, min(to_i, len(lst))), st)
            self._bump()

    def step_list(self, lst=None, prefix: str = "") -> list[str]:
        out = []
        for i, st in enumerate(self.task["steps"] if lst is None else lst):
            out.append(f"{prefix}{i}: {st['title']} [{st['status']}]")
            if st.get("substeps"):
                out += self.step_list(st["substeps"], f"{prefix}{i}.")
        return out

    def get_step(self, index) -> dict:
        lst, i, _ = self._locate(index)
        return lst[i]

    def clear_task(self) -> None:
        with self._lock:
            self._archive("cleared")
            self.task = {"goal": "", "steps": [], "started": None, "finished": None, "outcome": ""}
            self._bump()

    def finish_task(self, outcome: str, success: bool = True) -> dict:
        with self._lock:
            self.task["finished"] = time.time()
            self.task["outcome"] = outcome
            self.task["success"] = success
            for st in self._all_steps():
                if st["status"] == "active":
                    st["status"] = "done" if success else "failed"
                elif st["status"] == "pending":
                    st["status"] = "unreported" if success else "skipped"
            self.task.pop("_archived", None)
            self._archive("finished" if success else "stopped")
            self._bump()
            return self.task

    def progress(self) -> float:
        def frac(lst):
            if not lst:
                return 0.0
            tot = 0.0
            for st in lst:
                if st["status"] in ("done", "skipped", "unreported"):
                    tot += 1
                elif st.get("substeps"):
                    tot += frac(st["substeps"])
            return tot / len(lst)
        return frac(self.task["steps"])

    # ----- activity log ----------------------------------------------------
    def begin(self, kind: str, title: str, detail: str = "") -> int:
        with self._lock:
            entry = {"id": next(self._ids), "t": time.time(), "kind": kind, "title": title,
                     "detail": detail, "status": "running", "result": "", "duration": None}
            self.activity.append(entry)
            del self.activity[:-400]
            self.last_client_activity = time.time()
            self._bump()
            return entry["id"]

    def end(self, entry_id: int, status: str, result: str = "") -> None:
        with self._lock:
            for e in reversed(self.activity):
                if e["id"] == entry_id:
                    e["status"] = status
                    e["result"] = result
                    e["duration"] = time.time() - e["t"]
                    break
            self._bump()

    def note(self, text: str, kind: str = "note") -> None:
        i = self.begin(kind, text)
        self.end(i, "ok")

    # ----- images / radio --------------------------------------------------
    def add_image(self, name: str, path: str, caption: str) -> None:
        with self._lock:
            self.images.append({"name": name, "path": path, "caption": caption, "t": time.time()})
            del self.images[:-12]
            self._bump()

    def set_radio(self, **kw: Any) -> None:
        with self._lock:
            self.radio.update(kw)
            self._bump()

    # ----- dashboard chat --------------------------------------------------
    def chat_add(self, role: str, text: str, **meta: Any) -> int:
        """role: user (typed on the dashboard), ai (a Claude chat answering via dashboard_reply),
        agent (the dashboard agent), tool (a tool the agent called), system."""
        with self._lock:
            self._chat_rev += 1
            m = {"id": next(self._chat_ids), "t": time.time(), "role": role, "text": text, "rev": self._chat_rev, **meta}
            self.chat.append(m)
            del self.chat[:-300]
            if role != "user":
                self.last_client_activity = time.time()
            self._bump()
            return m["id"]

    def add_user_action(self, instruction: str, why: str = "", expect: str = "", options: list | None = None) -> dict:
        with self._lock:
            a = {"id": next(self._ua_ids), "t": time.time(), "instruction": instruction, "why": why, "expect": expect,
                 "options": list(options or []), "status": "waiting", "answer": "", "note": ""}
            self.user_actions.append(a)
            del self.user_actions[:-30]
            self._bump()
        self.chat_add("action", instruction, action_id=a["id"], why=why, expect=expect)
        return a

    def answer_user_action(self, aid: int, answer: str, note: str = "") -> dict:
        with self._lock:
            for a in self.user_actions:
                if a["id"] == int(aid):
                    a.update(status="answered", answer=str(answer)[:80], note=str(note)[:1000], answered_t=time.time())
                    self._bump()
                    break
            else:
                raise KeyError(f"no action {aid}")
        for m in reversed(self.chat):
            if m.get("action_id") == int(aid):
                self.chat_update(m["id"], answered=str(answer)[:80], answer_note=str(note)[:300])
                break
        return a

    def chat_detail(self, mid: int, **kw: Any) -> None:
        """Keep the full text of a tool call (arguments, result) for the chat's expand button - fetched on demand
        so the chat itself stays small."""
        with self._lock:
            d = self.chat_details.setdefault(int(mid), {})
            d.update({k: (v if not isinstance(v, str) else v[:60000]) for k, v in kw.items()})
            if len(self.chat_details) > 300:
                for k in sorted(self.chat_details)[:len(self.chat_details) - 300]:
                    self.chat_details.pop(k, None)

    def chat_update(self, mid: int, **kw: Any) -> None:
        with self._lock:
            for m in reversed(self.chat):
                if m["id"] == mid:
                    self._chat_rev += 1
                    m.update(kw, rev=self._chat_rev)
                    break
            self._bump()

    def chat_clear(self) -> None:
        with self._lock:
            self._chat_rev += 1
            self.chat = []
            self.chat_details = {}
            self.chat_state["cleared_rev"] = self._chat_rev
            self._bump()

    def set_chat_state(self, **kw: Any) -> None:
        with self._lock:
            self.chat_state.update(kw)
            self._bump()

    # ----- snapshot --------------------------------------------------------
    def _bump(self) -> None:
        self.version += 1

    def snapshot(self, since_activity: int = 0, chat_since: int = 0) -> dict:
        with self._lock:
            cleared = self.chat_state.get("cleared_rev", 0)
            return {
                "version": self.version,
                "uptime": time.time() - self.started,
                "task": {**self.task, "progress": self.progress()},
                "activity": [e for e in self.activity if e["id"] > since_activity][-200:],
                "images": [{"name": i["name"], "caption": i["caption"], "t": i["t"]} for i in self.images],
                "radio": dict(self.radio),
                "chat": [dict(m) for m in self.chat if m["rev"] > chat_since][-150:],
                "chat_rev": self._chat_rev,
                "chat_reset": chat_since < cleared,
                "chat_state": dict(self.chat_state, listening=self.chat_state.get("listening_until", 0) > time.time()),
                "user_actions": [dict(a) for a in self.user_actions if a["status"] == "waiting"],
            }

    def image_paths(self) -> list[str]:
        with self._lock:
            return [i["path"] for i in self.images]

    def last_activity_time(self) -> float:
        with self._lock:
            return self.activity[-1]["t"] if self.activity else self.started


BOARD = StatusBoard()


def _walk(steps):
    for x in steps or []:
        yield x
        yield from _walk(x.get("substeps") or [])


def load_task_history(path: str | None, limit: int = 20, search: str = "") -> list[dict]:
    """Past tasks (newest first, or best match first with a search), from every chat that shares the workspace."""
    if not path or not os.path.exists(path):
        return []
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
    words = [w for w in re.findall(r"\w+", (search or "").lower()) if len(w) > 1]
    if words:
        def score(r):
            hay = " ".join([r.get("goal", ""), r.get("outcome", "")] + [x.get("title", "") + " " + x.get("note", "")
                           for x in _walk(r.get("steps"))] + [n.get("text", "") for n in r.get("notes") or []]).lower()
            return sum(w in hay for w in words)
        recs = [(score(r), r) for r in recs]
        recs = [r for sc, r in sorted(recs, key=lambda x: (-x[0], -(x[1].get("finished") or 0))) if sc]
    else:
        recs.sort(key=lambda r: -(r.get("finished") or 0))
    out = []
    for r in recs[: max(1, int(limit))]:
        r = dict(r)
        r["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("started") or r.get("finished") or 0))
        out.append(r)
    return out
