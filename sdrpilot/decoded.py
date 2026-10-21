"""Decoded data in one fixed format: decoders print one JSON object per line prefixed with "SPDATA " (the
sp_data_sink block does it for any message port; epy blocks can call emit()). SDR Pilot picks these lines out of
a job's output, saves them to decoded.jsonl in the job folder, keeps a merged live state for the dashboard's
Decoded view, and gives the AI a compact summary instead of raw log text.

Record fields: kind (rds / adsb / aprs / packet / ...), t (unix time), plus whatever the decoder knows. A record
that names a station/device (key_field, or an icao / id / address / callsign field) becomes a row in a table;
RDS records merge into one station card; everything else is a message log."""
from __future__ import annotations

import collections
import json
import sys
import time

PREFIX = "SPDATA "
KEY_FIELDS = ("key", "icao", "id", "address", "addr", "mac", "callsign", "src", "source", "serial", "device_id")


def emit(kind: str, **fields) -> None:
    """For epy blocks / scripts: print one decoded record in SDR Pilot's format."""
    rec = {"kind": kind, "t": round(time.time(), 3), **fields}
    sys.stdout.write(PREFIX + json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse(line: str) -> dict | None:
    i = line.find(PREFIX)
    if i < 0:
        return None
    try:
        rec = json.loads(line[i + len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    rec.setdefault("kind", "data")
    rec.setdefault("t", time.time())
    return rec


def _key(rec: dict) -> str | None:
    kf = rec.get("key_field")
    if kf and rec.get(kf) not in (None, ""):
        return str(rec[kf])
    for k in KEY_FIELDS:
        if rec.get(k) not in (None, "") and not isinstance(rec.get(k), (dict, list)):
            return str(rec[k])
    return None


class Aggregator:
    """Live merged view of one job's decoded records."""

    def __init__(self, keep: int = 400):
        self.count = 0
        self.kinds: collections.Counter = collections.Counter()
        self.recent: collections.deque = collections.deque(maxlen=keep)
        self.rds: dict = {}
        self.rds_rt: list = []
        self.tables: dict[str, dict[str, dict]] = {}
        self.first_t = None
        self.last_t = None

    def add(self, rec: dict) -> None:
        self.count += 1
        rec = dict(rec)
        rec["seq"] = self.count
        kind = str(rec.get("kind") or "data")
        self.kinds[kind] += 1
        self.recent.append(rec)
        self.first_t = self.first_t or rec.get("t")
        self.last_t = rec.get("t")
        if kind == "rds":
            for k, v in rec.items():
                if k in ("kind", "t", "n", "seq", "key_field"):
                    continue
                self.rds[k] = v
                if k == "radiotext" and v and (not self.rds_rt or self.rds_rt[-1] != v):
                    last = self.rds_rt[-1] if self.rds_rt else ""
                    if last and (v.startswith(last) or last.startswith(v) or last.rstrip(". ") == v.rstrip(". ")):
                        self.rds_rt[-1] = max(v, last, key=len)  # the same text, still filling in
                    else:
                        self.rds_rt.append(v)
                    del self.rds_rt[:-10]
            self.rds["updated"] = rec.get("t")
            return
        key = _key(rec)
        if key is not None:
            tab = self.tables.setdefault(kind, {})
            row = tab.setdefault(key, {"_key": key, "_count": 0, "_first": rec.get("t")})
            for k, v in rec.items():
                if k in ("kind", "n", "seq", "key_field") or v in (None, "", "-"):
                    continue
                row[k] = v
            row["_count"] += 1
            row["_last"] = rec.get("t")
            if len(tab) > 500:  # forget the stalest
                for k in sorted(tab, key=lambda k: tab[k].get("_last") or 0)[:50]:
                    tab.pop(k, None)

    def since(self, seq: int, limit: int = 200) -> list[dict]:
        return [r for r in self.recent if r["seq"] > seq][-limit:]

    def view(self) -> dict:
        """Everything the dashboard needs to draw the Decoded view."""
        tables = {}
        for kind, tab in self.tables.items():
            rows = sorted(tab.values(), key=lambda r: -(r.get("_last") or 0))[:200]
            cols: list[str] = []
            for r in rows:
                for k in r:
                    if not k.startswith("_") and k not in cols and k not in ("t", "data"):
                        cols.append(k)
            tables[kind] = {"columns": cols[:14], "rows": rows}
        return {"count": self.count, "kinds": dict(self.kinds), "first_t": self.first_t, "last_t": self.last_t,
                "rds": ({**self.rds, "radiotext_history": list(self.rds_rt)} if self.rds else None),
                "tables": tables}

    def summary(self) -> dict:
        """Compact version for the AI's job results."""
        if not self.count:
            return {"count": 0}
        out: dict = {"count": self.count, "kinds": dict(self.kinds)}
        if self.rds:
            out["rds"] = {k: v for k, v in self.rds.items() if k in ("pi", "ps", "pty", "radiotext", "clock", "flags",
                                                                       "af")}
            if len(self.rds_rt) > 1:
                out["rds"]["radiotext_history"] = list(self.rds_rt)[-5:]
        for kind, tab in self.tables.items():
            rows = sorted(tab.values(), key=lambda r: -(r.get("_last") or 0))
            out.setdefault("tables", {})[kind] = {
                "rows": len(rows),
                "latest": [{k: v for k, v in r.items() if k not in ("_first", "t", "data") and v not in (None, "")}
                           for r in rows[:12]]}
        plain = [r for r in self.recent if r.get("kind") != "rds" and _key(r) is None]
        if plain:
            out["latest_messages"] = [{k: v for k, v in r.items() if k not in ("t", "n", "seq")} for r in plain[-8:]]
        return out
