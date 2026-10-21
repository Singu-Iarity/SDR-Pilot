"""Keeps tool results small for the AI without losing anything.

A result bigger than the limit is saved in full under workspace/state/results/<pid>/ and the AI gets a shortened
copy with the same structure: long lists keep their first items, long strings their start, and a "_full_result"
field says how to page through the complete original with get_full_result(id, path, offset)."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

MORE = "…"


def dumps(obj: Any) -> str:
    """Compact JSON: same data as indented JSON, ~20% fewer tokens."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _shrink(obj: Any, max_items: int, max_str: int) -> Any:
    if isinstance(obj, dict):
        return {k: _shrink(v, max_items, max_str) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [_shrink(v, max_items, max_str) for v in obj[:max_items]]
        if len(obj) > max_items:
            out.append(f"{MORE} +{len(obj) - max_items} more items")
        return out
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + f"{MORE} (+{len(obj) - max_str} chars)"
    return obj


def shrink(obj: Any, limit: int) -> Any:
    """Structure-preserving shortening until the compact JSON fits in `limit` characters."""
    for items, strs in ((40, 2000), (25, 1200), (15, 700), (10, 400), (6, 240), (4, 140), (3, 90), (2, 60), (1, 40)):
        s = _shrink(obj, items, strs)
        if len(dumps(s)) <= limit:
            return s
    return s


class ResultStore:
    def __init__(self, workspace: str, tag: str | None = None):
        self.root = os.path.join(workspace, "state", "results")
        self._lock = threading.Lock()
        self.retag(tag or str(os.getpid()))

    def retag(self, tag: str) -> None:
        """The agent host uses a fixed folder ("host") so a restarted host - whose conversation is restored - can
        still read the results that conversation points at. Numbering continues after the existing ones."""
        self.folder = os.path.join(self.root, tag)
        n = 0
        try:
            for fn in os.listdir(self.folder):
                if fn.startswith("R") and fn.endswith(".json") and fn[1:-5].isdigit():
                    n = max(n, int(fn[1:-5]))
        except OSError:
            pass
        self._n = n

    def put(self, text: str, tool: str = "") -> str:
        with self._lock:
            self._n += 1
            rid = f"R{self._n}"
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, rid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": rid, "tool": tool, "t": time.time(), "text": text}, f, ensure_ascii=False)
        return rid

    def _load(self, rid: str) -> dict:
        rid = rid.strip().upper()
        if not rid.startswith("R") or not rid[1:].isdigit():
            raise ValueError(f"bad result id {rid!r} (looks like 'R12')")
        p = os.path.join(self.folder, rid + ".json")
        if not os.path.exists(p):
            raise FileNotFoundError(f"no stored result {rid} in this SDR Pilot session")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def get(self, rid: str, path: str = "", offset: int = 0, limit: int = 8000) -> dict:
        d = self._load(rid)
        text = d["text"]
        sel = None
        if path:
            try:
                sel = json.loads(text)
            except ValueError:
                raise ValueError(f"{rid} is plain text; use offset/limit instead of path")
            for part in [p for p in path.replace("[", ".").replace("]", "").split(".") if p]:
                if isinstance(sel, list):
                    sel = sel[int(part)]
                elif isinstance(sel, dict):
                    if part not in sel:
                        raise KeyError(f"no '{part}' in {rid}{'.' + path if path else ''}; keys: {list(sel)[:30]}")
                    sel = sel[part]
                else:
                    raise KeyError(f"can't go into '{part}': value is a {type(sel).__name__}")
            text = sel if isinstance(sel, str) else dumps(sel)
        limit = max(500, min(int(limit), 30000))
        offset = max(0, int(offset))
        chunk = text[offset:offset + limit]
        out = {"id": rid, "tool": d.get("tool"), "path": path or None, "total_chars": len(text),
               "offset": offset, "text": chunk}
        if offset + limit < len(text):
            out["next_offset"] = offset + limit
        if not path and offset == 0:
            try:
                top = json.loads(d["text"])
                if isinstance(top, dict):
                    out["top_level_keys"] = {k: (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__)
                                             for k, v in top.items()}
            except ValueError:
                pass
        return out

    def fit(self, text: str, tool: str, limit: int) -> str:
        """Return `text` unchanged when it fits; otherwise store it and return a shortened stand-in."""
        if len(text) <= limit:
            return text
        rid = self.put(text, tool)
        note = {"id": rid, "chars": len(text),
                "how": f"shortened to save context; the complete result is kept - get_full_result(id='{rid}', "
                       "path='<field>' or offset=...) pages through it"}
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            small = shrink(obj, max(400, limit - 300))
            return dumps({**small, "_full_result": note})
        head = text[: max(200, limit - 300)]
        return head + f"\n{MORE} [{len(text) - len(head)} more chars - {note['how']}]"


def cleanup_leftovers(workspace: str, data_root: str, alive_pids: set[int], image_days: float = 14.0) -> dict:
    """Delete what chats that no longer exist left behind: their saved tool results, temp files, and old analysis
    images. Captures, flowgraphs and job outputs are never touched (they're your data)."""
    import shutil
    removed = {"result_folders": 0, "files": 0, "bytes": 0}

    def size(p: str) -> int:
        tot = 0
        for dp, _dn, fns in os.walk(p):
            for f in fns:
                try:
                    tot += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return tot
    root = os.path.join(workspace, "state", "results")
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if not os.path.isdir(p) or not name.isdigit() or int(name) in alive_pids:
                continue  # "host" and live chats keep theirs
            if time.time() - os.path.getmtime(p) < 120:
                continue
            removed["bytes"] += size(p)
            shutil.rmtree(p, ignore_errors=True)
            removed["result_folders"] += 1
    for sub in ("state", os.path.join("state", "sessions")):
        d = os.path.join(workspace, sub)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if fn.endswith(".tmp") and os.path.isfile(p) and time.time() - os.path.getmtime(p) > 600:
                    try:
                        removed["bytes"] += os.path.getsize(p)
                        os.remove(p)
                        removed["files"] += 1
                    except OSError:
                        pass
    img = os.path.join(data_root, "images")
    if image_days and os.path.isdir(img):
        cut = time.time() - image_days * 86400
        for fn in os.listdir(img):
            p = os.path.join(img, fn)
            try:
                if fn.endswith(".png") and os.path.getmtime(p) < cut:
                    removed["bytes"] += os.path.getsize(p)
                    os.remove(p)
                    removed["files"] += 1
            except OSError:
                pass
    return removed


class ContextPrefs:
    """Shared switches (workspace/state/context_prefs.json) that every SDR Pilot copy reads, set from the dashboard."""

    DEFAULTS = {"shorten_results": True, "result_max_chars": 12000, "task_reminders": True, "task_reminder_every": 12, "task_after_reply": True,
                "vision_hints": True, "cleanup": True, "image_days": 14}

    def __init__(self, workspace: str, defaults: dict | None = None):
        self.path = os.path.join(workspace, "state", "context_prefs.json")
        self.defaults = {**self.DEFAULTS, **(defaults or {})}
        self._cache: tuple[float, dict] = (-1.0, {})

    def get(self) -> dict:
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            return dict(self.defaults)
        if mt != self._cache[0]:
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            self._cache = (mt, d if isinstance(d, dict) else {})
        return {**self.defaults, **self._cache[1]}

    def set(self, **kw) -> dict:
        cur = self.get()
        for k, v in kw.items():
            if k == "shorten_results" and v is not None:
                cur[k] = bool(v)
            elif k == "result_max_chars" and v not in (None, ""):
                cur[k] = max(2000, min(int(v), 200000))
            elif k in ("vision_hints", "cleanup") and v is not None:
                cur[k] = bool(v)
            elif k == "image_days" and v not in (None, ""):
                cur[k] = max(0, min(int(float(v)), 3650))
            elif k == "task_after_reply" and v is not None:
                cur[k] = bool(v)
            elif k == "task_reminders" and v is not None:
                cur[k] = bool(v)
            elif k == "task_reminder_every" and v not in (None, ""):
                cur[k] = max(1, min(int(v), 50))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: cur[k] for k in self.DEFAULTS}, f, indent=1)
        os.replace(tmp, self.path)
        return cur

    def result_limit(self) -> int:
        p = self.get()
        return int(p["result_max_chars"]) if p.get("shorten_results") else 0
