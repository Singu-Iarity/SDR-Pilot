"""Out-of-tree (OOT) GNU Radio modules: find them (CGRAN, conda-forge, GitHub), look inside them (which blocks,
which are complete one-block receivers), and install them into the user's GNU Radio (radioconda) - conda-forge
packages first (prebuilt, safest), pip for Python-only modules, a CMake build from source as the last resort.

Policy (the user's rule): SDR Pilot's point is that the AI builds decoders it understands out of blocks. A module
whose block does the whole job in one go ("lora_rx", "wifi_rx", ...) may only be used when the AI explains why
building it from components would be extremely complicated - that justification is recorded and shown to the user.
Component blocks (demodulators, sync, FEC, framing) are fine."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

UA = "SDR-Pilot (+https://github.com/; GNU Radio OOT search)"
ALL_IN_ONE_RE = re.compile(r"(^|_)(rx|receiver|receive|decoder|decode|demodulator_and|full_rx|transceiver|"
                           r"rx_chain|phy_rx|sniffer|monitor)(_|$)", re.I)


def _get(url: str, timeout: float = 20, accept: str = "") -> bytes:
    h = {"User-Agent": UA}
    if accept:
        h["Accept"] = accept
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        h["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def _gh(path: str) -> dict | list:
    return json.loads(_get("https://api.github.com" + path, accept="application/vnd.github+json"))


def _words(q: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", q.lower()) if len(w) > 1 and w not in ("gr", "gnuradio", "the", "and",
                                                                                       "for", "decoder", "module")]


# ------------------------------------------------------------------ search
def _cgran(q: str, cache_dir: str) -> list[dict]:
    path = os.path.join(cache_dir, "cgran.html")
    try:
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 86400:
            data = _get("https://www.cgran.org/", 25)
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        with open(path, encoding="utf-8", errors="replace") as f:
            page = f.read()
    except Exception:
        return []
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if len(cells) < 3:
            continue
        link = re.search(r'href="(/?\d+/?)"', row)
        txt = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]
        txt = [" ".join(t.split()) for t in txt]
        name = txt[0]
        rest = " ".join(txt[1:]).lower()
        out.append({"name": name, "source": "CGRAN", "description": txt[2] if len(txt) > 2 else "",
                    "categories": txt[3] if len(txt) > 3 else "", "gnuradio_versions": txt[4] if len(txt) > 4 else "",
                    "last_commit": txt[1] if len(txt) > 1 else "",
                    "page": "https://www.cgran.org" + ("/" + link.group(1).strip("/") + "/" if link else ""),
                    "_text": (name + " " + rest).lower()})
    w = _words(q)
    hits = [o for o in out if all(x in o["_text"] for x in w)] or [o for o in out if any(x in o["_text"] for x in w)]
    for o in hits:
        o.pop("_text", None)
    return hits[:15]


def _conda(q: str) -> list[dict]:
    out = []
    try:
        d = _gh("/search/repositories?q=" + urllib.parse.quote(f"gnuradio in:name org:conda-forge") + "&per_page=100")
        names = [i["name"][:-len("-feedstock")] for i in d.get("items", []) if i["name"].endswith("-feedstock")]
    except Exception:
        names = []
    try:  # anaconda.org's own index (more complete when reachable)
        d = json.loads(_get("https://api.anaconda.org/search?name=gnuradio", 15))
        names += [p["name"] for p in d if isinstance(p, dict) and p.get("owner") == "conda-forge"]
    except Exception:
        pass
    w = _words(q)
    exe = Installs.conda_exe()
    if exe:  # the user's own conda knows both channels radioconda uses (conda-forge + ryanvolz)
        for x in w[:2]:
            try:
                kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
                r = subprocess.run([exe, "search", "--json", "-c", "conda-forge", "-c", "ryanvolz", f"*gnuradio*{x}*"],
                                   capture_output=True, text=True, timeout=90, **kw)
                d = json.loads(r.stdout or "{}")
                names += [k for k in d if isinstance(d.get(k), list)]
            except Exception:
                pass
    for x in w[:3]:  # the search index misses some feedstocks: ask for the likely names directly
        for cand in (f"gnuradio-{x}", f"gnuradio-{x}_sdr", f"gnuradio-{x}-sdr"):
            if cand in names:
                continue
            try:
                _gh(f"/repos/conda-forge/{cand}-feedstock")
                names.append(cand)
            except Exception:
                pass
    for n in sorted(set(names)):
        if n == "gnuradio" or not n.startswith("gnuradio-"):
            continue
        if any(x in n.lower() for x in w):
            out.append({"name": n, "source": "conda (conda-forge / ryanvolz)",
                        "install": f"oot_install('{n}') - prebuilt conda package (preferred)",
                        "page": f"https://anaconda.org/search?q={n}"})
    return out


def _github(q: str) -> list[dict]:
    try:
        d = _gh("/search/repositories?q=" + urllib.parse.quote(f"{q} gnuradio") + "&sort=stars&per_page=12")
    except Exception as e:
        return [{"source": "GitHub", "error": f"search failed: {e}"}]
    out = []
    for i in d.get("items", []):
        out.append({"name": i["name"], "repo": i["html_url"], "source": "GitHub", "stars": i.get("stargazers_count"),
                    "last_push": (i.get("pushed_at") or "")[:10], "description": (i.get("description") or "")[:200],
                    "topics": i.get("topics", [])[:8]})
    return out


def search(query: str, cache_dir: str) -> dict:
    res = {"query": query, "conda_forge": _conda(query), "cgran": _cgran(query, cache_dir), "github": _github(query)}
    res["how"] = ("Prefer a conda-forge package (prebuilt for radioconda). Check what a module contains with "
                  "oot_inspect before installing: SDR Pilot wants decoders built from component blocks - a module "
                  "that only offers a one-block receiver needs a justification (oot_install).")
    return res


# ------------------------------------------------------------------ inspect
def _repo_of(target: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:/|$|\?|#)", target + "/")
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", target.strip())
    return (m.group(1), m.group(2)) if m else None


def _conda_source(pkg: str) -> str | None:
    """conda-forge package -> its upstream GitHub repo (from the feedstock recipe)."""
    for f in ("recipe/meta.yaml", "recipe/recipe.yaml"):
        try:
            txt = _get(f"https://raw.githubusercontent.com/conda-forge/{pkg}-feedstock/main/{f}").decode("utf-8", "replace")
        except Exception:
            continue
        m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+", txt)
        if m:
            return m.group(0)
    return None


def _yml_ports(yml: str, key: str) -> list[dict]:
    m = re.search(rf"^{key}:\s*\n((?:[ \t-].*\n?)*)", yml, re.M)
    if not m:
        return []
    ports = []
    for chunk in re.split(r"\n\s*-\s+", "\n" + m.group(1)):
        if not chunk.strip():
            continue
        dom = re.search(r"domain:\s*(\w+)", chunk)
        dt = re.search(r"dtype:\s*([\w$.{}]+)", chunk)
        ports.append({"domain": dom.group(1) if dom else "stream", "dtype": dt.group(1) if dt else ""})
    return ports


def classify_block(bid: str, label: str, inputs: list[dict], outputs: list[dict]) -> bool:
    """True for a block that looks like a whole receiver: raw samples in, decoded messages/bytes out."""
    raw_in = any(p["domain"] == "stream" and (p["dtype"] in ("complex", "fc32", "float", "") or "$" in p["dtype"])
                 for p in inputs)
    decoded_out = not outputs or any(p["domain"] == "message" or p["dtype"] in ("byte", "u8") for p in outputs)
    return bool(raw_in and decoded_out and (ALL_IN_ONE_RE.search(bid) or ALL_IN_ONE_RE.search(label or "")))


# well-known out-of-tree modules (module name -> GitHub repo): a fallback when GitHub's search can't find them
KNOWN_MODULES = {
    "lte": "kit-cel/gr-lte", "lora_sdr": "tapparelj/gr-lora_sdr", "lora": "rpp0/gr-lora",
    "ieee802_11": "bastibl/gr-ieee802-11", "ieee802-11": "bastibl/gr-ieee802-11",
    "ieee802_15_4": "bastibl/gr-ieee802-15-4", "ieee802-15-4": "bastibl/gr-ieee802-15-4",
    "satellites": "daniestevez/gr-satellites", "adsb": "mhostetter/gr-adsb", "rds": "bastibl/gr-rds",
    "gsm": "ptrkrysik/gr-gsm", "iridium": "muccc/gr-iridium", "inspector": "gnuradio/gr-inspector",
    "osmosdr": "osmocom/gr-osmosdr", "fosphor": "osmocom/gr-fosphor", "iqbal": "osmocom/gr-iqbal",
    "radar": "kit-cel/gr-radar", "foo": "bastibl/gr-foo", "air_modes": "bistromath/gr-air-modes",
    "air-modes": "bistromath/gr-air-modes", "ais": "bistromath/gr-ais", "tpms": "jboone/gr-tpms",
    "dsd": "argilo/gr-dsd", "elster": "argilo/gr-elster", "tempest": "git-artes/gr-tempest",
    "dvbs2rx": "igorauad/gr-dvbs2rx", "fhss_utils": "sandialabs/gr-fhss_utils",
    "pdu_utils": "sandialabs/gr-pdu_utils", "nordic": "BastilleResearch/gr-nordic", "paint": "drmpeg/gr-paint",
    "funcube": "dl1ksv/gr-funcube", "dect2": "pavelyazev/gr-dect2",
}


def resolve(target: str) -> dict:
    """Whatever the AI calls a module - 'gr-lte', 'lte', 'gnuradio-lora_sdr', 'github.com/x/gr-y', a full URL,
    'owner/repo' - to something installable: {"conda": "gnuradio-..."} or {"repo": "owner/name"}, plus the other
    candidates found so the choice is visible."""
    t = str(target or "").strip().strip("'\"").rstrip("/")
    if not t:
        raise ValueError("no module given")
    if re.fullmatch(r"gnuradio-[\w.-]+", t):
        return {"conda": t}
    rp = _repo_of(t)
    if rp and ("github" in t or "/" in t):
        return {"repo": f"{rp[0]}/{rp[1]}"}
    name = re.sub(r"\s+", "-", t.lower())
    core = re.sub(r"^(gr-|gnuradio-)", "", name)
    out: dict = {"resolved_from": t}
    for cand in (f"gnuradio-{core}", f"gnuradio-{core.replace('-', '_')}"):  # a prebuilt conda package?
        try:
            _gh(f"/repos/conda-forge/{cand}-feedstock")
            out["conda"] = cand
            break
        except Exception:
            continue
    cands = []
    names = {name, f"gr-{core}", core, f"gr_{core}", f"gnuradio-{core}", f"gr-{core.replace('_', '-')}",
             f"gr-{core.replace('-', '_')}"}
    # repo descriptions say "GNU Radio" as often as "gnuradio": try a few searches until one finds the name
    for q in (f"{core} in:name gnuradio", f"gr-{core} in:name", f"{core} in:name \"GNU Radio\"",
              f"gr-{core} in:name,description,readme"):
        try:
            d = _gh("/search/repositories?q=" + urllib.parse.quote(q) + "&sort=stars&per_page=30")
        except Exception as e:
            out["search_error"] = str(e)[:200]
            break
        for i in d.get("items", []):
            if i["name"].lower() in names and i["full_name"] not in {c["repo"] for c in cands}:
                cands.append({"repo": i["full_name"], "stars": i.get("stargazers_count", 0),
                              "last_push": (i.get("pushed_at") or "")[:10],
                              "description": (i.get("description") or "")[:140]})
        if cands:
            break
    if not cands:  # well-known modules: works even when GitHub's search is rate-limited or misses
        known = KNOWN_MODULES.get(core) or KNOWN_MODULES.get(core.replace("-", "_"))
        if known:
            cands.append({"repo": known, "stars": None, "last_push": "", "description": "SDR Pilot's list of "
                          "well-known GNU Radio modules"})
            out.pop("search_error", None)
    cands.sort(key=lambda c: (-(c["stars"] or 0), c["last_push"]), reverse=False)
    if cands:
        out.setdefault("repo", cands[0]["repo"])
        out["candidates"] = cands[:6]
    if "conda" not in out and "repo" not in out:
        if out.get("search_error"):
            raise ValueError(f"couldn't look up '{t}' (GitHub: {out['search_error']}). Pass the module's GitHub URL "
                             "(from oot_search or web_search) or its conda package name 'gnuradio-...'.")
        raise ValueError(f"found no GNU Radio module called '{t}' (no conda package gnuradio-{core}, no GitHub repo "
                         f"named gr-{core}). Use oot_search('{core}') and pass a result's repo URL or conda name.")
    return out


def inspect(target: str) -> dict:
    """Blocks of a module (GitHub repo URL / owner/repo / conda-forge package name / just its name like 'gr-lte'),
    which ones are one-block receivers, and how to install it."""
    conda_pkg = None
    res = resolve(target)
    t = res.get("conda") or res["repo"]
    if res.get("conda") and res.get("repo"):
        t = res["conda"]
    if t.startswith("gnuradio-") and "/" not in t:
        conda_pkg = t
        src = _conda_source(t)
        if not src:
            return {"target": t, "conda_package": t, "note": "couldn't find its source repo to list the blocks; it "
                    "can still be installed (oot_install method='conda')"}
        t = src
    rp = _repo_of(t)
    if not rp:
        raise ValueError("give a GitHub repo (https://github.com/owner/gr-name or owner/gr-name) or a conda-forge "
                         "package name (gnuradio-xyz)")
    owner, repo = rp
    try:
        info = _gh(f"/repos/{owner}/{repo}")
    except Exception as e:  # GitHub's API unreachable / rate-limited: still installable from source
        return {"repo": f"https://github.com/{owner}/{repo}", **({"resolved_from": res["resolved_from"]}
                if res.get("resolved_from") else {}),
                "note": f"GitHub's API isn't answering ({str(e)[:100]}), so the blocks can't be listed here - "
                        f"oot_install(target='{owner}/{repo}') still works (it clones the source and builds it)"}
    branch = info.get("default_branch") or "main"
    out = {"repo": info.get("html_url"), "description": info.get("description"), "stars": info.get("stargazers_count"),
           "last_push": (info.get("pushed_at") or "")[:10], "default_branch": branch}
    if res.get("resolved_from"):
        out["resolved_from"] = res["resolved_from"]
    if res.get("candidates") and len(res["candidates"]) > 1:
        out["other_candidates"] = res["candidates"][1:]
    try:
        branches = [b["name"] for b in _gh(f"/repos/{owner}/{repo}/branches?per_page=100")]
        out["branches"] = [b for b in branches if re.search(r"3\.?10|3\.?9|main|master|maint", b)][:10]
    except Exception:
        pass
    root = []
    try:
        root = [x["name"] for x in _gh(f"/repos/{owner}/{repo}/contents?ref={branch}")]
    except Exception:
        pass
    out["build"] = ("C++ (needs CMake + a compiler to build from source)" if "lib" in root or "include" in root else
                    "Python blocks (CMake install, no compiler)" if "python" in root else "unknown")
    blocks, aio = [], []
    try:
        ymls = [x for x in _gh(f"/repos/{owner}/{repo}/contents/grc?ref={branch}") if x["name"].endswith(".yml")]
    except Exception:
        ymls = []
    for x in ymls[:60]:
        try:
            yml = _get(x["download_url"], 15).decode("utf-8", "replace")
        except Exception:
            continue
        bid = (re.search(r"^id:\s*(\S+)", yml, re.M) or [None, x["name"].replace(".block.yml", "")])[1]
        label = (re.search(r"^label:\s*(.+)$", yml, re.M) or [None, ""])[1].strip().strip("'\"")
        ins, outs = _yml_ports(yml, "inputs"), _yml_ports(yml, "outputs")
        one = classify_block(bid, label, ins, outs)
        tx = bool(re.search(r"(^|_)(tx|transmit|modulat|mapper|encoder|whitening|add_crc)", bid, re.I)) and not one
        blocks.append({"id": bid, "label": label, "in": [p["domain"] + ":" + p["dtype"] for p in ins][:4],
                       "out": [p["domain"] + ":" + p["dtype"] for p in outs][:4],
                       **({"one_block_receiver": True} if one else {}), **({"transmit_side": True} if tx else {})})
        if one:
            aio.append(bid)
    out["blocks"] = blocks
    out["one_block_receivers"] = aio
    out["component_blocks"] = len([b for b in blocks if not b.get("one_block_receiver")])
    try:
        rd = _get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md", 15).decode("utf-8", "replace")
        out["readme"] = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", rd)[:1500]
    except Exception:
        pass
    pkg = conda_pkg or ("gnuradio-" + re.sub(r"^gr-", "", repo).lower().replace("_", "-"))
    out["conda_package_guess"] = pkg
    if aio and out["component_blocks"] < 2:
        out["policy"] = (f"This module mostly offers a one-block receiver ({aio}). Build the decoder from standard "
                         "blocks instead, unless that would be extremely complicated - then pass a justification to "
                         "oot_install explaining exactly why.")
    elif aio:
        out["policy"] = (f"Use its component blocks to build the decoder. The one-block receiver(s) {aio} need a "
                         "justification (oot_install(..., use_one_block=[...], justification=...)) before a "
                         "flowgraph may use them.")
    if not (conda_pkg or res.get("conda")):
        out["conda_package"] = None
        out["install_hint"] = ("No prebuilt conda package. oot_install(target='" + (out.get("repo") or t) + "', "
                               "method='source') builds it with CMake - C++ modules need a compiler and usually fail "
                               "on Windows (WSL works); method='pip' only suits Python-only modules.")
    br = " ".join(out.get("branches") or []) + " " + str(out.get("default_branch"))
    if out.get("last_push", "9999") < "2021" and not re.search(r"3\.?10|3\.?9|main", br):
        out["compatibility"] = (f"last change {out.get('last_push')} and no 3.9/3.10 branch: probably written for "
                                "GNU Radio 3.7/3.8 - it won't build against this 3.10 install without porting. Look "
                                "for a maintained fork (other_candidates / oot_search) or a different approach.")
    return out


# ------------------------------------------------------------------ compatibility
def _branch_rank(name: str, major: str) -> int:
    n = name.lower()
    mj = major.replace(".", r"\.?")
    if major and re.search(rf"(maint|gr|v|release)?[-_]?{mj}\b", n):
        return 0
    if n in ("main", "master", "develop", "dev"):
        return 2
    from .ootbuild import vtuple
    if major and vtuple(major) >= (3, 9) and re.search(r"3\.?(9|10|11)", n):
        return 1
    return 9


def pick_branch(owner: str, repo: str, gr_installed: str) -> tuple[str, str, list[str]]:
    """The branch most likely to build against the installed GNU Radio."""
    from .ootbuild import gr_major
    try:
        info = _gh(f"/repos/{owner}/{repo}")
    except Exception as e:  # GitHub's API unreachable / rate-limited: clone the default branch
        return "", f"the default branch (GitHub API unavailable: {str(e)[:80]})", []
    default = info.get("default_branch") or "main"
    try:
        names = [b["name"] for b in _gh(f"/repos/{owner}/{repo}/branches?per_page=100")]
    except Exception:
        names = [default]
    major = gr_major(gr_installed)
    best = sorted(names, key=lambda n: (_branch_rank(n, major), n != default))
    if best and _branch_rank(best[0], major) == 0:
        return best[0], f"branch made for GNU Radio {major}", names
    if best and _branch_rank(best[0], major) == 1 and _branch_rank(default, major) > 1:
        return best[0], "branch for a newer GNU Radio (3.9+)", names
    return default, "the default branch", names


def find_forks(owner: str, repo: str, gr_installed: str, limit: int = 6) -> list[dict]:
    """Forks that look ported to GNU Radio 3.9+ (pybind11 bindings or a 3.9/3.10 branch), newest first."""
    from .ootbuild import gr_major
    major = gr_major(gr_installed)
    forks = _gh(f"/repos/{owner}/{repo}/forks?sort=newest&per_page=40")
    forks = [f for f in forks if (f.get("pushed_at") or "") >= "2021"][:limit * 2]
    forks.sort(key=lambda f: (-(f.get("stargazers_count") or 0), f.get("pushed_at") or ""), reverse=False)
    out = []
    for f in forks[:limit]:
        full = f["full_name"]
        try:
            names = [b["name"] for b in _gh(f"/repos/{full}/branches?per_page=100")]
        except Exception:
            continue
        cand = sorted(names, key=lambda n: _branch_rank(n, major))
        for br in cand[:2]:
            try:
                py = [x["name"] for x in _gh(f"/repos/{full}/contents/python?ref={urllib.parse.quote(br)}")]
            except Exception:
                py = []
            has_bind = "bindings" in py
            if not has_bind:
                for sub in py[:3]:
                    try:
                        if "bindings" in [x["name"] for x in _gh(f"/repos/{full}/contents/python/{sub}?ref="
                                                                  f"{urllib.parse.quote(br)}")]:
                            has_bind = True
                            break
                    except Exception:
                        continue
            if has_bind or _branch_rank(br, major) <= 1:
                out.append({"repo": full, "branch": br, "pushed": (f.get("pushed_at") or "")[:10],
                            "stars": f.get("stargazers_count"),
                            "why": "pybind11 bindings (3.9+)" if has_bind else f"branch '{br}'"})
                break
    return out


def _rmtree(path: str) -> bool:
    """Delete a folder even when Windows marks files read-only (git's object files): -> True if it's gone."""
    import stat

    def onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if os.path.exists(path):
        try:
            shutil.rmtree(path, onerror=onerr)
        except Exception:
            pass
    return not os.path.exists(path)


def _module_root(path: str) -> str:
    """The folder with the module's top CMakeLists.txt (a zip or a nested checkout can put it one level down)."""
    if os.path.exists(os.path.join(path, "CMakeLists.txt")):
        return path
    for depth in range(2):
        found = []
        for dp, dn, fn in os.walk(path):
            dn[:] = [d for d in dn if not d.startswith(".") and d not in ("build",)]
            if dp[len(path):].count(os.sep) > depth + 1:
                dn[:] = []
                continue
            if "CMakeLists.txt" in fn and re.search(r"project\s*\(", open(os.path.join(dp, "CMakeLists.txt"),
                                                                          encoding="utf-8", errors="replace").read(),
                                                     re.I):
                found.append(dp)
        if found:
            return sorted(found, key=len)[0]
    return path


# ------------------------------------------------------------------ install
class Installs:
    """Background installs (conda can take minutes) with logs; and the registry of one-block receivers +
    the justifications that allow them."""

    def __init__(self, workspace: str, log_dir: str):
        self.reg_path = os.path.join(workspace, "state", "oot_registry.json")
        self.log_dir = log_dir
        self.jobs: dict[str, dict] = {}
        self._n = 0

    def registry(self) -> dict:
        try:
            with open(self.reg_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"modules": {}, "one_block": {}}

    def _save(self, reg: dict) -> None:
        os.makedirs(os.path.dirname(self.reg_path), exist_ok=True)
        tmp = self.reg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1)
        os.replace(tmp, self.reg_path)

    def record(self, name: str, info: dict, justification: str, allowed: list[str]) -> None:
        reg = self.registry()
        reg["modules"][name] = {"t": time.time(), "repo": info.get("repo"), "method": info.get("method"),
                                "one_block_receivers": info.get("one_block_receivers", [])}
        for b in info.get("one_block_receivers", []):
            ent = reg["one_block"].setdefault(b, {"module": name, "allowed": False})
            if b in allowed and justification:
                ent.update(allowed=True, justification=justification, t=time.time())
        self._save(reg)

    def allow(self, block: str, justification: str) -> None:
        reg = self.registry()
        reg["one_block"].setdefault(block, {"module": "?"}).update(allowed=True, justification=justification,
                                                                   t=time.time())
        self._save(reg)

    def blocked(self, types: list[str]) -> list[str]:
        reg = self.registry().get("one_block", {})
        return [t for t in types if t in reg and not reg[t].get("allowed")]

    @staticmethod
    def conda_exe() -> str | None:
        pref = sys.prefix
        cands = ([os.path.join(pref, "Scripts", n) for n in ("mamba.exe", "conda.exe")] +
                 [os.path.join(pref, "condabin", "conda.bat"), os.path.join(pref, "condabin", "mamba.bat")] +
                 [os.path.join(pref, "bin", n) for n in ("mamba", "conda")])
        for c in cands:
            if os.path.exists(c):
                return c
        return shutil.which("mamba") or shutil.which("conda")

    def start(self, name: str, method: str, target: str, branch: str = "", port: str = "auto",
              src_dir: str = "", extra: dict | None = None) -> dict:
        self._n += 1
        jid = f"I{self._n}"
        os.makedirs(self.log_dir, exist_ok=True)
        logp = os.path.join(self.log_dir, f"oot_install_{time.strftime('%Y%m%d_%H%M%S')}_{jid}.log")
        job = {"id": jid, "name": name, "method": method, "state": "running", "stage": "starting", "log": logp,
               "started": time.time(), "returncode": None, "report": {}, "port": port}
        if src_dir:
            job["src"] = src_dir
        if extra:
            job.update(extra)
        self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, method, target, branch), daemon=True).start()
        return job

    def _cmd(self, job: dict, cmd: list[str] | None, cwd: str | None = None, env: dict | None = None,
             note: str = "") -> int:
        with open(job["log"], "a", encoding="utf-8", errors="replace") as lf:
            if note:
                lf.write(f"# {note}\n")
            if not cmd:
                return 0
            lf.write("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
            lf.flush()
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
            p = subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, **kw)
            job["pid"] = p.pid
            rc = p.wait()
            lf.write(f"# exit code {rc}\n")
            return rc

    def _stage(self, job: dict, stage: str, msg: str = "") -> None:
        job["stage"] = stage
        self._cmd(job, None, note=f"== {stage}" + (f": {msg}" if msg else ""))

    def _run(self, job: dict, method: str, target: str, branch: str) -> None:
        rc = 1
        try:
            if method == "conda":
                exe = self.conda_exe()
                if not exe:
                    raise RuntimeError("couldn't find conda/mamba in this Python's environment (radioconda)")
                self._stage(job, "install", target)
                rc = self._cmd(job, [exe, "install", "-y", "-p", sys.prefix, "-c", "conda-forge", "-c", "ryanvolz",
                                     target])
            elif method == "pip":
                self._stage(job, "install", target)
                rc = self._cmd(job, [sys.executable, "-m", "pip", "install", "--no-input", f"git+{target}"
                                     + (f"@{branch}" if branch else "")])
            elif method == "source":
                rc = self._build_source(job, target, branch)
            elif method == "rebuild":
                rc = self._configure_build_install(job, job["src"], job.get("analysis") or {})
            else:
                raise ValueError(f"unknown method {method}")
        except Exception as e:
            with open(job["log"], "a", encoding="utf-8") as lf:
                lf.write(f"\nERROR: {type(e).__name__}: {e}\n")
            job["error"] = f"{type(e).__name__}: {e}"
            rc = 1
        job["returncode"] = rc
        job["state"] = "installed" if rc == 0 else "failed"
        job["ended"] = time.time()
        if rc != 0:
            from . import ootbuild as ob
            try:
                with open(job["log"], encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            job["report"]["errors"] = ob.error_summary(txt)
            job["report"]["advice"] = ob.advice(txt)
        self._save_job(job)

    def _save_job(self, job: dict) -> None:
        if job.get("work"):
            try:
                with open(os.path.join(job["work"], "sdrpilot_build.json"), "w", encoding="utf-8") as f:
                    json.dump({k: v for k, v in job.items() if k != "pid"}, f, indent=1, default=str)
            except OSError:
                pass

    # ---------------------------------------------------------------- source builds
    def _fetch(self, job: dict, owner: str, repo: str, br: str, work: str) -> str:
        """Get the source into a fresh folder under work (one per repo/branch, so a fork never lands inside an
        earlier download) and return the module's top folder (the one with the CMakeLists.txt)."""
        dest = os.path.join(work, "src" if not job.get("fetched") else
                            f"src_{owner}_{re.sub(r'[^\w.-]', '_', br or 'default')}")
        job["fetched"] = int(job.get("fetched") or 0) + 1
        if not _rmtree(dest):  # couldn't clear it (Windows file locks): use a new folder instead
            dest = f"{dest}_{int(time.time())}"
        git = shutil.which("git")
        if git:
            rc = self._cmd(job, [git, "clone", "--depth", "1", "--recursive", *(["--branch", br] if br else []),
                                 f"https://github.com/{owner}/{repo}.git", dest])
            if rc == 0:
                return _module_root(dest)
            if not _rmtree(dest):
                dest = f"{dest}_{int(time.time())}"
        zpath = os.path.join(work, f"src_{owner}_{repo}.zip")
        data, err = None, None
        for b in ([br] if br else ["master", "main"]):
            try:
                data = _get(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}", 180)
                break
            except Exception as e:
                err = e
        if data is None:
            raise RuntimeError(f"couldn't download {owner}/{repo}: {err}")
        with open(zpath, "wb") as f:
            f.write(data)
        import zipfile
        tmp = os.path.join(work, f"unzip_{int(time.time())}")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        inner = next(os.path.join(tmp, d) for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)))
        if os.path.exists(dest):
            dest = f"{dest}_{int(time.time())}"
        shutil.move(inner, dest)
        _rmtree(tmp)
        self._cmd(job, None, note=f"downloaded {owner}/{repo}@{br} as a zip")
        return _module_root(dest)

    def _build_source(self, job: dict, repo_url: str, branch: str) -> int:
        from . import ootbuild as ob
        rep = job["report"]
        gr_inst = ob.installed_gr_version()
        rep["gnuradio_installed"] = gr_inst or "unknown"
        local = os.path.isdir(repo_url)
        base = os.path.join(self.log_dir, "..", "oot_build")
        if local:
            name = os.path.basename(os.path.abspath(repo_url).rstrip("/\\"))
            work = os.path.abspath(os.path.join(base, f"{name}_local"))
            _rmtree(work)
            os.makedirs(work, exist_ok=True)
            self._stage(job, "fetch", f"copying {repo_url}")
            src = os.path.join(work, "src")
            shutil.copytree(repo_url, src, ignore=shutil.ignore_patterns("build", ".git"))
        else:
            rp = _repo_of(repo_url)
            if not rp:
                raise ValueError("source installs need a GitHub repository URL (or a folder on this PC)")
            owner, repo = rp
            self._stage(job, "fetch", f"{owner}/{repo}")
            br, why, _all = pick_branch(owner, repo, gr_inst) if not branch else (branch, "asked for", [])
            rep["branch"] = br or "(default)"
            rep["branch_why"] = why
            work = os.path.abspath(os.path.join(base, f"{repo}_{re.sub(r'[^\w.-]', '_', br or 'default')}"))
            _rmtree(work)
            os.makedirs(work, exist_ok=True)
            src = self._fetch(job, owner, repo, br, work)
        job["work"] = work
        self._stage(job, "analyze")
        an = ob.analyze(src)
        rep["analysis"] = an
        porting = ob.needs_port(an, gr_inst)
        self._cmd(job, None, note=f"module '{an['name']}', {an['language']}, written for GNU Radio {an['written_for']} "
                                  f"({'; '.join(an['evidence'])}); installed GNU Radio {gr_inst}")
        if porting and not local and an["language"] == "C++":
            self._stage(job, "compatibility", "looking for a fork already ported to this GNU Radio")
            try:
                forks = find_forks(owner, repo, gr_inst)
            except Exception as e:
                forks = []
                self._cmd(job, None, note=f"fork search failed: {e}")
            rep["ported_forks"] = forks[:5]
            for fk in forks[:2]:
                fo, fr = fk["repo"].split("/")
                try:
                    fsrc = self._fetch(job, fo, fr, fk["branch"], work)
                except Exception as e:
                    self._cmd(job, None, note=f"couldn't fetch fork {fk['repo']}: {e}")
                    continue
                fan = ob.analyze(fsrc)
                if not ob.needs_port(fan, gr_inst):
                    src, an, porting = fsrc, fan, False
                    rep["used_fork"] = f"{fk['repo']}@{fk['branch']} ({fk['why']})"
                    rep["analysis"] = an
                    self._cmd(job, None, note=f"using the ported fork {rep['used_fork']}")
                    break
        if porting and job.get("port") == "never":
            raise RuntimeError(f"this module was written for GNU Radio {an['written_for']} and the installed one is "
                               f"{gr_inst}; no ported fork found, and port='never'")
        # compiler (C++ only)
        env = dict(os.environ)
        if an["language"] == "C++":
            menv = ob.msvc_env()
            for line in ob.MSVC_REPORT:
                self._cmd(job, None, note=f"compiler: {line}")
            if menv is None:
                raise RuntimeError("this is a C++ module and no usable C++ compiler environment was found (checked: "
                                   + "; ".join(ob.MSVC_REPORT[-6:]) + "). On Windows it needs Microsoft's "
                                   "C++ Build Tools: ask the user (user_action) to install 'Build Tools for Visual "
                                   "Studio 2022' with the 'Desktop development with C++' workload, then retry. "
                                   "(Alternative: a prebuilt conda package, or GNU Radio under WSL.)")
            env = menv
        job["env_kind"] = "msvc" if os.name == "nt" and an["language"] == "C++" else "default"
        # dependencies
        self._stage(job, "dependencies")
        pk, unknown = ob.conda_packages(an, porting)
        # build tools that are already there (Visual Studio's CMake/Ninja, a CMake on PATH...) don't need conda -
        # and every conda package costs a slow dependency check
        have_tools = [t for t in ("cmake", "ninja", "pkg-config") if shutil.which(t, path=env.get("PATH"))]
        if have_tools:
            pk = [o for o in pk if o[0] not in have_tools]
            self._cmd(job, None, note=f"already available, not installed with conda: {', '.join(have_tools)}")
        rep["dependencies_unknown"] = unknown
        rep["dependencies"] = self.conda_deps(job, pk, gr_inst)
        job["analysis"] = an
        if an["language"] == "Python":
            self._stage(job, "install", "Python-only module: copying files (no compiler needed)")
            rep["installed"] = ob.install_python_only(src, an, lambda c, cwd=None, note="": self._cmd(job, c, cwd, env, note))
            job["src"] = src
            return self._verify(job, an)
        if porting:
            self._stage(job, "port", f"{an['written_for']} -> {ob.gr_major(gr_inst)}")
            src, prep = ob.port_module(src, an, work, lambda c, cwd=None, note="": self._cmd(job, c, cwd, env, note))
            rep["port"] = prep
        job["src"] = src
        self._save_job(job)
        return self._configure_build_install(job, src, an, env)

    def _configure_build_install(self, job: dict, src: str, an: dict, env: dict | None = None) -> int:
        from . import ootbuild as ob
        if env is None:
            env = ob.msvc_env() if job.get("env_kind") == "msvc" else dict(os.environ)
            if env is None:
                raise RuntimeError("the C++ build tools are no longer found")
        cmake = shutil.which("cmake", path=env.get("PATH")) or shutil.which("cmake") or \
            os.path.join(ob.library_prefix(), "bin", "cmake.exe" if os.name == "nt" else "cmake")
        if not os.path.exists(cmake):
            raise RuntimeError("CMake isn't installed - oot_build(action='deps', packages=['cmake','ninja']) first")
        build = os.path.join(src, "build")
        prefix = ob.library_prefix()
        ninja = shutil.which("ninja", path=env.get("PATH")) or shutil.which("ninja")
        self._stage(job, "configure")
        rc = self._cmd(job, [cmake, "-S", src, "-B", build, *(["-G", "Ninja"] if ninja else []),
                             f"-DCMAKE_INSTALL_PREFIX={prefix}", f"-DCMAKE_PREFIX_PATH={prefix}",
                             "-DCMAKE_BUILD_TYPE=Release", f"-DGR_PYTHON_DIR={ob.python_dir()}",
                             f"-DPython_EXECUTABLE={sys.executable}", f"-DPYTHON_EXECUTABLE={sys.executable}",
                             "-DENABLE_DOXYGEN=OFF", "-DENABLE_TESTING=OFF",
                             # CMake 4 refuses projects that declare a minimum below 3.5 (old modules' Find files)
                             "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-Wno-dev"], cwd=src, env=env)
        if rc == 0:
            self._stage(job, "build")
            # Ninja: keep compiling after the first failure, so one rebuild shows every file that needs fixing
            rc = self._cmd(job, [cmake, "--build", build, "--config", "Release", "--parallel",
                                 *(["--", "-k", "0"] if ninja else [])], cwd=src, env=env)
        if rc == 0:
            self._stage(job, "install")
            rc = self._cmd(job, [cmake, "--install", build, "--config", "Release"], cwd=src, env=env)
        if rc != 0:
            return rc
        return self._verify(job, an)

    def _verify(self, job: dict, an: dict) -> int:
        from . import ootbuild as ob
        from .compat import child_env
        self._stage(job, "verify", f"import gnuradio.{an.get('name')}")
        v = ob.verify(an.get("name") or job["name"], child_env())
        job["report"]["verify"] = v
        self._cmd(job, None, note=f"verify: imports={v['imports']} grc files={len(v['grc_block_files'])}"
                                  + (f" error: {v.get('import_error')}" if not v["imports"] else ""))
        return 0 if v["imports"] else 4

    def _conda_plan(self, exe: str, want: list[str], pins: list[str]) -> tuple[str, object]:
        """Dry run -> ("ok", None) | ("missing", [names]) | ("blocked", [protected packages it'd change]) |
        ("error", message)."""
        from . import ootbuild as ob
        try:
            r = subprocess.run([exe, "install", "--dry-run", "--json", "-y", "--freeze-installed", "-p", sys.prefix,
                                "-c", "conda-forge", *want, *pins], capture_output=True, text=True, timeout=900)
            txt = r.stdout[r.stdout.find("{"):] if "{" in r.stdout else "{}"
            j = json.loads(txt)
        except Exception as e:
            return "error", f"dry run failed: {e}"
        if j.get("error") or j.get("exception_name"):
            msg = str(j.get("error") or j.get("message") or "")
            missing = [w for w in want if re.search(rf"(?<![\w-]){re.escape(w.split('=')[0])}(?![\w-])", msg)]
            return ("missing", missing) if missing else ("error", msg[:400])
        unlink = [a.get("name") for a in (j.get("actions") or {}).get("UNLINK", [])]
        bad = sorted({n for n in unlink if n and ob.PROTECTED.match(n)})
        return ("blocked", bad) if bad else ("ok", None)

    def conda_deps(self, job: dict, pk: list[list[str]], gr_inst: str = "") -> dict:
        """Install the missing conda-forge packages without touching GNU Radio's environment: every library GNU
        Radio was built with is pinned to the version installed now, development packages (libboost-devel ...) are
        pinned to the version of the library already there, and a package that would still change something is
        skipped on its own instead of blocking the rest."""
        from . import ootbuild as ob
        out: dict = {"installed": [], "already": [], "not_on_conda": [], "blocked": {}}
        exe = self.conda_exe()
        if not exe:
            out["note"] = "conda/mamba not found - dependencies weren't checked (the build will say what's missing)"
            self._cmd(job, None, note=out["note"])
            return out
        try:
            r = subprocess.run([exe, "list", "--json", "-p", sys.prefix], capture_output=True, text=True, timeout=300)
            installed = {x["name"]: x.get("version", "") for x in json.loads(r.stdout)}
        except Exception:
            installed = {}
        want, alts = [], {}
        for options in pk:
            if any(o in installed for o in options):
                out["already"].append(next(o for o in options if o in installed))
                continue
            want.append(options[0])
            alts[options[0]] = list(options[1:])

        def spec(name: str) -> str:
            # headers / dev packages must match the library GNU Radio already uses (libboost 1.86 -> libboost-devel
            # 1.86), or conda "upgrades" the whole stack to match a newer one
            base = re.sub(r"-(devel|headers|dev)$", "", name)
            for cand in (base, "lib" + base, base.replace("lib", "", 1)):
                if cand != name and installed.get(cand):
                    return f"{name}={installed[cand]}"
            return name
        pins = [f"{n}={v}" for n, v in installed.items() if v and ob.PROTECTED.match(n)]
        for _attempt in range(8):
            if not want:
                break
            specs = [spec(w) for w in want]
            self._cmd(job, None, note=f"dry run: conda install {' '.join(specs)} (+{len(pins)} pinned packages)")
            st, info = self._conda_plan(exe, specs, pins)
            if st == "missing":
                for w in [w for w in want if spec(w) in info or w in info]:
                    want.remove(w)
                    if alts.get(w):
                        nxt = alts[w].pop(0)
                        alts[nxt] = alts[w]
                        want.append(nxt)
                    else:
                        out["not_on_conda"].append(w)
                continue
            if st in ("blocked", "error") and len(want) > 1:
                # find which ones are the problem: check them one at a time, keep the rest
                ok = []
                for w in want:
                    st1, info1 = self._conda_plan(exe, [spec(w)], pins)
                    if st1 == "ok":
                        ok.append(w)
                    elif st1 == "missing":
                        out["not_on_conda"].append(w)
                    else:
                        out["blocked"][w] = info1 if isinstance(info1, list) else str(info1)[:200]
                        self._cmd(job, None, note=f"skipping {w}: " + (f"it would change {info1}" if isinstance(
                            info1, list) else str(info1)[:200]))
                if not ok:
                    break
                want = ok
                continue
            if st == "blocked":
                out["blocked"] = {w: info for w in want}
                self._cmd(job, None, note=f"NOT installing {want}: it would change {info}")
                break
            if st == "error":
                out["note"] = "conda couldn't plan the install: " + str(info)
                break
            specs = [spec(w) for w in want]
            rc = self._cmd(job, [exe, "install", "-y", "--freeze-installed", "-p", sys.prefix, "-c", "conda-forge",
                                 *specs, *pins])
            if rc == 0:
                out["installed"] = specs
            else:
                out["note"] = f"conda install failed (exit {rc}) - see the log"
            break
        if out["blocked"]:
            out["blocked_why"] = ("these would replace parts of GNU Radio's own environment, so they were left out; "
                                  "the build may still work without them (or with the versions already installed)")
        return out

    def status(self, jid: str = "") -> list[dict]:
        js = [self.jobs[jid]] if jid else list(self.jobs.values())
        out = []
        for j in js:
            tail = ""
            try:
                with open(j["log"], encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-25:])
            except OSError:
                pass
            rec = {k: v for k, v in j.items() if k not in ("pid", "report", "analysis", "env_kind", "work")}
            rep = j.get("report") or {}
            for k in ("gnuradio_installed", "branch", "branch_why", "used_fork", "dependencies", "dependencies_unknown",
                      "verify", "errors", "advice", "installed"):
                if rep.get(k):
                    rec[k] = rep[k]
            an = rep.get("analysis") or {}
            if an:
                rec["module"] = {k: an.get(k) for k in ("name", "written_for", "language")}
            if rep.get("port"):
                rec["port"] = {"changes": len(rep["port"].get("changes", [])),
                               "warnings": rep["port"].get("warnings", [])[:10]}
            if j.get("state") == "running":
                rec["next"] = (f"STILL RUNNING (stage: {j.get('stage')}) - this is not a result yet. Call "
                               f"oot_status(id='{j['id']}', wait_s=110) again until state is 'installed' or "
                               "'failed'; source builds take 5-30 minutes. Closing this chat stops the install.")
            if j.get("state") == "failed" and j.get("src"):
                rec["next"] = (f"oot_build(action='report', id='{j['id']}') for the details, then read/patch the "
                               "sources and oot_build(action='rebuild')")
            out.append(rec | {"log_tail": tail if j.get("state") != "installed" else tail[-600:],
                              "running_s": round((j.get("ended") or time.time()) - j["started"], 1)})
        return out
