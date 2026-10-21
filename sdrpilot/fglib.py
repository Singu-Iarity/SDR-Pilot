"""Keeps the saved-flowgraph folder useful: every flowgraph carries a health record (did it validate, did it ever
run, did it decode anything), listings put working ones first and keep broken experiments out of other AIs' way,
and prune() archives the junk - failed tries, unfinished drafts, exact duplicates, and the _v2/_final/_fixed
attempts a working version has replaced. Archived flowgraphs are moved, never deleted (restore() brings one back).

health: works (decoded something) | ran_ok (ran cleanly, wrote output) | ran_nothing (ran cleanly, no output) |
        failed (crashed) | invalid (doesn't validate) | draft (unfinished) | untested (valid, never run)"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time

ARCHIVE = "_archive"
GOOD = ("works", "ran_ok")
RANK = {"works": 0, "ran_ok": 1, "untested": 2, "ran_nothing": 3, "draft": 4, "failed": 5, "invalid": 6}
HEALTH_KEYS = ("health", "last_run", "runs", "worked_ever", "health_note")
_SUFFIX = re.compile(r"(?:_(?:v?\d+[a-z]?|final|fixed|fix|manual|clean|bare|bones|simple|attempt|trial|test|new|old|"
                     r"copy|try|retry|again|working|alt|b|c|redo|real|proper|basic|minimal))+$")


def family(name: str) -> str:
    """'rds_decoder_93200_v19', 'rds_decoder_final_v6' -> 'rds_decoder'; 'wifi_trial_003a' -> 'wifi'."""
    f = name.lower()
    while True:
        g = _SUFFIX.sub("", f)
        if g == f or not g:
            return f
        f = g


def spec_digest(spec: dict | None) -> str:
    s = dict(spec or {})
    for k in ("name", "title", "description"):
        s.pop(k, None)
    return hashlib.sha1(json.dumps(s, sort_keys=True, default=str).encode()).hexdigest()[:16]


def carry_health(old: dict | None, new: dict) -> dict:
    """When a flowgraph is rebuilt: keep its run record if the design didn't change (e.g. rebuilt for a retune),
    otherwise it starts over as untested / invalid / draft."""
    if old and old.get("spec_digest") and old.get("spec_digest") == new.get("spec_digest") and new.get("valid"):
        for k in HEALTH_KEYS:
            if k in old:
                new[k] = old[k]
        if new.get("health") in ("invalid", "draft", None):
            new["health"] = "untested"
        return new
    new["health"] = "invalid" if not new.get("valid") else ("draft" if new.get("draft") else "untested")
    if old and old.get("worked_ever"):
        new["previous_version_worked"] = True
    return new


def _meta_path(fg_dir: str, name: str) -> str:
    return os.path.join(fg_dir, name, "sdrpilot.json")


def read(fg_dir: str, name: str) -> dict | None:
    try:
        with open(_meta_path(fg_dir, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def update(fg_dir: str, name: str, **fields) -> None:
    p = _meta_path(fg_dir, name)
    m = read(fg_dir, name)
    if m is None:
        return
    m.update(fields)
    tmp = p + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    os.replace(tmp, p)


def record_run(fg_dir: str, name: str, status: str, decoded: int, outputs: list, runtime_s: float, job_id: str,
               error: str = "") -> str:
    """Called when a job ends: work out the flowgraph's health from what the run produced."""
    m = read(fg_dir, name)
    if m is None:
        return ""
    out_bytes = sum(int(o.get("bytes") or 0) for o in outputs if o.get("file") != "decoded.jsonl")
    if status in ("failed", "finished_with_errors"):
        health = "failed"
    elif decoded > 0:
        health = "works"
    elif out_bytes > 1024:
        health = "ran_ok"
    else:
        health = "ran_nothing"
    if health == "ran_nothing" and m.get("health") in GOOD and runtime_s < 5:
        health = m["health"]  # a very short run proves nothing either way
    m["health"] = health
    m["runs"] = int(m.get("runs") or 0) + 1
    m["worked_ever"] = bool(m.get("worked_ever") or health in GOOD)
    m["last_run"] = {"t": time.time(), "job": job_id, "status": status, "decoded": decoded,
                     "output_bytes": out_bytes, "runtime_s": round(runtime_s, 1), "error": error[:300]}
    update(fg_dir, name, **{k: m[k] for k in ("health", "runs", "worked_ever", "last_run")})
    return health


def entries(fg_dir: str, running: set | None = None) -> list[dict]:
    out = []
    if not os.path.isdir(fg_dir):
        return out
    for name in os.listdir(fg_dir):
        if name == ARCHIVE:
            continue
        m = read(fg_dir, name)
        if m is None:
            continue
        folder = os.path.join(fg_dir, name)
        try:
            mtime = max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder))
        except (OSError, ValueError):
            mtime = 0
        health = m.get("health") or ("invalid" if m.get("valid") is False else
                                     "draft" if m.get("draft") else "untested")
        lr = m.get("last_run") or {}
        out.append({"name": name, "kind": m.get("kind", "grc"), "health": health, "hier": bool(m.get("hier")),
                    "valid": m.get("valid", True), "draft": bool(m.get("draft")), "mtime": mtime,
                    "built": m.get("built"), "runs": int(m.get("runs") or 0), "worked_ever": bool(m.get("worked_ever")),
                    "last_run": lr, "family": family(name),
                    "digest": m.get("spec_digest") or spec_digest(m.get("spec")) if m.get("spec") else
                    "py:" + str(m.get("py")),
                    "title": (m.get("spec") or {}).get("title") or m.get("description") or "",
                    "built_for": m.get("built_for") or "", "live": bool(m.get("uses_live_radio")),
                    "running": name in (running or set())})
    out.sort(key=lambda e: (RANK.get(e["health"], 9), -e["mtime"]))
    return out


def describe(e: dict) -> str:
    lr = e.get("last_run") or {}
    h = e["health"]
    txt = {"works": f"works (decoded {lr.get('decoded', '?')} items last run)", "ran_ok": "ran cleanly, wrote output",
           "ran_nothing": "ran but decoded/wrote nothing", "failed": "crashed when run",
           "invalid": "doesn't validate", "draft": "unfinished draft", "untested": "valid, never run"}.get(h, h)
    if h == "failed" and lr.get("error"):
        txt += f": {lr['error'][:100]}"
    return txt


def candidates(fg_dir: str, running: set | None = None, older_than_h: float = 1.0) -> list[dict]:
    """What prune() would archive, with the reason. Never: running flowgraphs, working ones (unless an exact
    duplicate of another working one), reusable blocks that validate, anything touched in the last older_than_h."""
    es = entries(fg_dir, running)
    now = time.time()
    old = lambda e: now - e["mtime"] > older_than_h * 3600
    picks: dict[str, str] = {}
    for e in es:
        if e["running"] or not old(e) or (e["hier"] and e["health"] != "invalid"):
            continue
        if e["health"] == "invalid":
            picks[e["name"]] = "doesn't validate"
        elif e["health"] == "draft":
            picks[e["name"]] = "unfinished draft"
        elif e["health"] == "failed" and not e["worked_ever"]:
            picks[e["name"]] = "crashed when run and never worked"
    by_digest: dict[str, list] = {}
    for e in es:
        by_digest.setdefault(e["digest"], []).append(e)
    for group in by_digest.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda e: (RANK.get(e["health"], 9), -e["mtime"]))
        keep = group[0]
        for e in group[1:]:
            if not e["running"] and old(e) and not e["hier"] and e["name"] not in picks:
                picks[e["name"]] = f"exact duplicate of {keep['name']}"
    by_fam: dict[str, list] = {}
    for e in es:
        by_fam.setdefault(e["family"], []).append(e)
    for fam, group in by_fam.items():
        good = [e for e in group if e["health"] in GOOD]
        if not good or len(group) < 2:
            continue
        best = sorted(good, key=lambda e: (RANK[e["health"]], -e["mtime"]))[0]
        for e in group:
            if e is best or e["running"] or e["hier"] or not old(e) or e["name"] in picks:
                continue
            if e["health"] not in GOOD:
                picks[e["name"]] = f"earlier attempt - {best['name']} works ({describe(best)})"
    out = []
    for e in es:
        if e["name"] in picks:
            out.append({"name": e["name"], "health": e["health"], "reason": picks[e["name"]],
                        "age_h": round((now - e["mtime"]) / 3600, 1)})
    return out


def archive(fg_dir: str, names: list[str]) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest_root = os.path.join(fg_dir, ARCHIVE, stamp)
    moved, failed = [], {}
    for n in names:
        src = os.path.join(fg_dir, n)
        if not os.path.isfile(os.path.join(src, "sdrpilot.json")):
            failed[n] = "no such flowgraph"
            continue
        try:
            os.makedirs(dest_root, exist_ok=True)
            shutil.move(src, os.path.join(dest_root, n))
            moved.append(n)
        except OSError as e:
            failed[n] = str(e)
    return {"archived": moved, "failed": failed, "archive_folder": dest_root if moved else None}


def archived(fg_dir: str) -> list[dict]:
    root = os.path.join(fg_dir, ARCHIVE)
    out = []
    if os.path.isdir(root):
        for stamp in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, stamp)
            if os.path.isdir(d):
                for n in sorted(os.listdir(d)):
                    out.append({"name": n, "archived": stamp, "path": os.path.join(d, n)})
    return out


def restore(fg_dir: str, name: str) -> str:
    hit = next((a for a in archived(fg_dir) if a["name"] == name), None)
    if not hit:
        raise FileNotFoundError(f"'{name}' isn't in the archive")
    dest = os.path.join(fg_dir, name)
    if os.path.exists(dest):
        raise FileExistsError(f"a flowgraph called '{name}' exists again - rename or archive it first")
    shutil.move(hit["path"], dest)
    return dest


# ------------------------------------------------------------------ notes any AI can leave on any flowgraph
# (its own, another chat's, a recipe, an imported or hand-made .grc): what it does, quirks, what worked, what didn't -
# so the next AI doesn't have to rediscover it. Kept in workspace/state/flowgraph_notes.json.
NOTE_KINDS = ("note", "quirk", "works_with", "fails_with", "howto", "result")
_NOTES_LOCK = __import__("threading").Lock()


def note_key(name: str, is_recipe: bool = False) -> str:
    if is_recipe:
        return "recipe:" + name
    if name.lower().endswith(".grc") or os.sep in name or "/" in name:
        return "file:" + os.path.normcase(os.path.abspath(name))
    return name


def load_notes(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_notes(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def edit_notes(path: str, key: str, note: str = "", kind: str = "note", description: str = "", who: str = "",
               remove: int | None = None) -> dict:
    with _NOTES_LOCK:
        data = load_notes(path)
        ent = data.setdefault(key, {"description": "", "notes": []})
        if description:
            ent["description"] = description.strip()[:2000]
            ent["described_by"] = who
            ent["described_t"] = time.time()
        if note:
            ent["notes"].append({"t": time.time(), "kind": kind if kind in NOTE_KINDS else "note",
                                 "text": note.strip()[:2000], "who": who})
            del ent["notes"][:-60]
        if remove is not None:
            if not 0 <= int(remove) < len(ent["notes"]):
                raise ValueError(f"no note #{remove} (there are {len(ent['notes'])})")
            ent["notes"].pop(int(remove))
        if not ent["description"] and not ent["notes"]:
            data.pop(key, None)
        _save_notes(path, data)
        return data.get(key) or {"description": "", "notes": []}


def notes_for(path: str, key: str, brief: bool = False) -> dict | None:
    ent = load_notes(path).get(key)
    if not ent:
        return None
    if not brief:
        return {"description": ent.get("description", ""),
                "notes": [{"#": i, "kind": n["kind"], "text": n["text"],
                           "when": time.strftime("%Y-%m-%d", time.localtime(n["t"]))}
                          for i, n in enumerate(ent.get("notes", []))]}
    out = {}
    if ent.get("description"):
        out["description"] = ent["description"][:240]
    quirks = [n["text"][:160] for n in ent.get("notes", []) if n["kind"] in ("quirk", "fails_with")]
    if quirks:
        out["quirks"] = quirks[-3:]
    if ent.get("notes"):
        out["notes"] = len(ent["notes"])
    return out or None
