"""Dashboard agent: a Claude conversation that lives inside SDR Pilot, so you can talk to it from the dashboard
(also from your phone over Tailscale) without a Claude chat open. It calls the Anthropic Messages API with your
own API key and uses exactly the same SDR Pilot tools a Claude chat would, in-process.

Runs only in the SDR Pilot host process (run_server.py --host)."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("sdrpilot.agent")

# Provider presets. "anthropic" speaks the Anthropic Messages API; everything else the OpenAI-compatible
# Chat Completions API, which OpenAI, Google Gemini, OpenRouter, LM Studio, Ollama, vLLM, Groq... all offer.
PRESETS = {
    "anthropic": {"label": "Anthropic (Claude)", "provider": "anthropic", "base_url": "https://api.anthropic.com",
                  "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
                  "key_env": "ANTHROPIC_API_KEY", "needs_key": True, "vision": True},
    "openai": {"label": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1", "models": [],
               "key_env": "OPENAI_API_KEY", "needs_key": True, "vision": True},
    "gemini": {"label": "Google Gemini", "provider": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "models": [],
               "key_env": "GEMINI_API_KEY", "needs_key": True, "vision": True},
    "openrouter": {"label": "OpenRouter (any model)", "provider": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "models": [], "key_env": "OPENROUTER_API_KEY", "needs_key": True, "vision": True},
    "lmstudio": {"label": "LM Studio (local model)", "provider": "openai", "base_url": "http://127.0.0.1:1234/v1",
                 "models": [], "key_env": "", "needs_key": False, "vision": False},
    "ollama": {"label": "Ollama (local model)", "provider": "openai", "base_url": "http://127.0.0.1:11434/v1",
               "models": [], "key_env": "", "needs_key": False, "vision": False},
    "custom": {"label": "Other OpenAI-compatible API", "provider": "openai", "base_url": "", "models": [],
               "key_env": "", "needs_key": False, "vision": False},
}
EXCLUDE = {"open_dashboard", "dashboard_listen", "dashboard_reply"}
KEEP_IMAGES = 3            # older tool images are replaced by a placeholder to save tokens
CHARS_PER_TOKEN = 3.5      # rough, for budgeting before the provider reports real numbers
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")
DEFAULT_SETTINGS = {"context_tokens": 0, "tool_detail": "auto", "compact_history": True, "nudge": True, "stream": True,
                    "auto_recover": True, "max_reply_tokens": 16384,
                    "custom_instructions": "", "show_reasoning": True}
THOUGHT_MAX = 12000   # characters of a model's reasoning kept for the dashboard (the end, if longer)


def _split_think(s: str) -> tuple[str, str]:
    """Reasoning some models write into the reply itself: '<think>...</think>answer' (or just '...</think>answer'
    when the chat template opened the tag). Returns (reasoning, the rest)."""
    if not s:
        return "", s
    m = re.match(r"\s*<think>(.*?)(</think>|$)(.*)", s, re.S)
    if m:
        return m.group(1), m.group(3) if m.group(2) else ""
    if "</think>" in s and "<think>" not in s:
        a, b = s.split("</think>", 1)
        return a, b
    return "", s
MAX_REPLY_CAP = 65536
# errors servers return when the prompt doesn't fit the model's context
CONTEXT_ERR_RE = re.compile(r"context (length|window|size|limit)|n_ctx|too many tokens|maximum context|prompt is too long|"
                            r"exceed(s|ed)? (the )?(context|max|maximum)|tokens to keep|context_length_exceeded|"
                            r"reduce the length|input is too long|too long for the context|context overflow", re.I)


def _is_repetitive(text: str) -> bool:
    """A reply stuck in a loop compresses extremely well; real content (even a big flowgraph spec) doesn't."""
    if len(text) < 2000:
        return False
    import zlib
    tail = text[-12000:].encode("utf-8", "replace")
    return len(zlib.compress(tail, 6)) / max(1, len(tail)) < 0.08


def _context_limit_from(msg: str) -> int | None:
    """The model's context size, if the error message states it."""
    for pat in (r"n_ctx(?:_slot)?\s*[=:]\s*(\d{3,7})", r"context (?:length|window|size)(?: is| of)?\s*\(?(\d{3,7})",
                r"maximum context length is (\d{3,7})", r">\s*(\d{3,7}) maximum", r"(\d{3,7})[- ]token context"):
        m = re.search(pat, msg, re.I)
        if m:
            return int(m.group(1))
    return None
TOOL_HELP = {"name": "tool_help", "description": "Full description, parameter details and examples of any SDR Pilot "
             "tool (the descriptions you see are shortened to save context). Call it before using a tool you're unsure of.",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "tool name"}},
                              "required": ["name"]}}
# a reply that says what it will do next but makes no tool call ("Next I'll tune to 433.92 MHz.")
INTENT_RE = re.compile(r"\b(I'?ll|I will|I'm going to|I am going to|let me|let's|next,? I|now I|I need to|I should|"
                       r"going to|will now|proceed(ing)? to|next step)\b", re.I)

# ...but only a concrete, doable next step is worth a nudge. These say the model has (rightly) stopped: it's blocked,
# waiting on the user, handing over a decision, done, or explaining why it can't go further.
STOP_RE = re.compile(
    r"\b(can'?t|cannot|can not|unable to|not able to|isn'?t possible|not possible|no way to|impossible|blocked|"
    r"won'?t work|doesn'?t work|didn'?t work|gave up|giving up|i'?ll stop|stopping (here|now)|stop here|out of ideas|"
    r"nothing (more|else) (i|to|that)|no further|not worth|beyond what|outside (of )?what|not something i|"
    r"requires? (physical|hardware|a different|another|you|the user)|needs? you to|need your|"
    r"waiting (for|on) (you|the user|your)|wait(ing)? until you|once you('ve| have)?|after you('ve| have)?|"
    r"when you('ve| have)?|your (call|choice|decision)|up to you|let me know|tell me (which|what|if|whether)|"
    r"if you('d| would)? (like|prefer|rather)|if you want|would you like|do you want|want me to|shall i|"
    r"(task|request|job|everything|that) (is |'s )?(now )?(done|complete|completed|finished)|all done|that'?s (all|it)|"
    r"i'?m (done|finished)|finished|completed the|nothing left|no more (steps|work)|"
    r"next time|later on|in a (future|new|later) (session|chat)|another time|"
    r"isn'?t (supported|available|installed)|not (supported|available|installed)|no (signal|data) (to|left)|"
    r"don'?t have (access|a way|the)|shouldn'?t|should not|not safe|not advisable)\b", re.I)
# the announced step is something a tool can actually do now
ACTION_RE = re.compile(
    r"\b(tune|retune|capture|record|scan|sweep|run|re-?run|build|rebuild|add|edit|fix|check|look|inspect|measure|"
    r"analy[sz]e|plot|decode|demodulate|install|search|try|test|set|switch|open|start|restart|stop the|compute|zoom|"
    r"listen|monitor|call|use|read|fetch|compare|connect|lower|raise|increase|reduce|adjust|change|create|write|"
    r"update|verify|confirm|grab|take|narrow|widen|filter|extract|save|load|import)\w*\b", re.I)

AGENT_PROMPT = """

You are the SDR Pilot dashboard agent. The user talks to you through the chat box of the SDR Pilot dashboard - often
from a phone over Tailscale - and sees your text replies there, next to your live tool activity, the task card, the
live waterfall and the flowgraph view. Keep replies short and concrete (a few sentences or a small list; no long
essays) and say what you found, with frequencies and numbers. Ask before anything that takes long or retunes a radio
another chat is using. ACT, DON'T ANNOUNCE: when you decide on a next step, make the tool call in that same reply -
never end a reply with "next I'll ..." unless you are stopping to ask the user a question. Keep going with tool
calls until the request is done, then give the result. Stopping is also the right call when you're genuinely blocked
(missing hardware/software, a physical change only the user can make, nothing useful left to try, or going on would
be unsafe or pointless): then stop, say plainly what's blocking you and what would unblock it - an honest stop beats
busywork. SAVE ROUND-TRIPS: when you already know several calls you need (marking task steps, a task_note plus a
task_step, setting a few variables, tune + snapshot), make them together - several tool calls in one reply, or one
batch(calls=[{"tool": ..., "args": {...}}, ...]) call. task_step also takes a list of indexes (index=[0, 1]) for the
same change to several steps. Each user message starts with the local time."""


def _short(v, n=90) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


REPORT_TEXT = ("📋 Status report, please: in 2-4 sentences, what are you working on right now, what have you found or "
               "changed so far, and what's your next step? Then carry on with the task.")



def lmstudio_models(root: str, headers: dict | None = None, timeout: float = 6) -> list[dict]:
    """Every model LM Studio has downloaded, loaded or not, from its native REST API: v1 (LM Studio 0.4+,
    /api/v1/models) or, for older versions, v0. Normalised to {id, type, loaded, instances, vision, max_ctx,
    loaded_ctx, quant}. [] if this isn't LM Studio."""
    h = {k: v for k, v in (headers or {}).items() if k.lower() == "authorization"}  # v1 API tokens
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{root}/api/v1/models", headers=h), timeout=timeout) as r:
            d = json.loads(r.read())
        out = []
        for m in d.get("models", []):
            if not isinstance(m, dict) or not m.get("key"):
                continue
            caps = m.get("capabilities") or {}
            vision = bool(caps.get("vision")) if isinstance(caps, dict) else "vision" in [str(x).lower() for x in caps]
            inst = [i for i in m.get("loaded_instances") or [] if isinstance(i, dict)]
            q = m.get("quantization")
            out.append({"id": m["key"], "type": m.get("type") or "llm", "loaded": bool(inst),
                        "instances": [str(i.get("id")) for i in inst if i.get("id")], "vision": vision,
                        "max_ctx": m.get("max_context_length"),
                        "loaded_ctx": next(((i.get("config") or {}).get("context_length") for i in inst
                                            if (i.get("config") or {}).get("context_length")), None),
                        "quant": q.get("name") if isinstance(q, dict) else q})
        if out or "models" in d:
            return out
    except Exception:
        pass
    try:  # LM Studio before 0.4: REST API v0
        with urllib.request.urlopen(f"{root}/api/v0/models", timeout=timeout) as r:
            d = json.loads(r.read())
        out = []
        for m in d.get("data", []):
            if not isinstance(m, dict) or not m.get("id"):
                continue
            caps = [str(x).lower() for x in m.get("capabilities") or []]
            loaded = m.get("state") == "loaded"
            out.append({"id": m["id"], "type": "embedding" if m.get("type") == "embeddings" else m.get("type") or "llm",
                        "loaded": loaded, "instances": [], "vision": m.get("type") == "vlm" or "vision" in caps,
                        "max_ctx": m.get("max_context_length"), "loaded_ctx": m.get("loaded_context_length"),
                        "quant": m.get("quantization")})
        return out
    except Exception:
        return []

class AgentError(Exception):
    pass


class _StreamUnsupported(Exception):
    pass


class DashboardAgent:
    def __init__(self, cfg, board, mcp, system_prompt: str):
        self.cfg, self.board, self.mcp = cfg, board, mcp
        self.system_prompt = system_prompt
        self.path = os.path.join(cfg.workspace, "state", "agent.json")
        self.messages: list[dict] = []
        self.queue: list[tuple[int, str]] = []
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.busy = False
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
        self.last_context = 0
        self.last_active = time.time()
        self._vision_auto: dict = {}   # (base_url, model) -> the server says it's a vision model
        self._tools: list[dict] | None = None
        self._tools_full: dict[str, str] = {}
        self._tools_key = None
        self._budget_cache = None  # (key, tokens, source, t)
        self._warned_small = False
        self.compacted = 0
        self.infer: dict | None = None   # live inference progress (prompt processing / generating), for the dashboard
        self.last_call: dict | None = None
        self._prev_req = ""              # last request's messages, to estimate how much of the prompt is cached
        self._prompt_tps = None          # measured prompt-processing speed (tokens/s)
        self._chars_per_token = CHARS_PER_TOKEN
        self._no_stream = False
        self._no_progress_flag = False
        self._live_mid = None
        self._thought_mid = None
        self._max_out = 16384
        self._pub_t = 0.0
        self._lms = None                 # LM Studio server-log reader (exact prompt progress), started when needed
        self.report_pending = False      # the user pressed "report": answer at the next step boundary
        self._no_tools_once = False
        self.on_reset = None             # callback from the server: forget per-conversation state
        self.conv_path = os.path.join(cfg.workspace, "state", "agent_conversation.json")
        self._restore()
        self.results = None  # ResultStore, set by the server (full copies of shortened results)
        self.prefs = None    # ContextPrefs (dashboard switches shared by all SDR Pilot copies)
        self.publish()

    # ------------------------------------------------------------------ conversation survives restarts
    def _save(self) -> None:
        try:
            tmp = self.conv_path + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved": time.time(), "messages": self.messages[-400:]}, f, ensure_ascii=False)
            os.replace(tmp, self.conv_path)
        except Exception:  # pragma: no cover
            log.exception("saving the agent conversation failed")

    def _restore(self) -> None:
        """Pick up the conversation a previous SDR Pilot host had (e.g. after start_host.bat restarted it) and show
        it on the dashboard again."""
        try:
            with open(self.conv_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        msgs = d.get("messages") or []
        if not msgs or time.time() - d.get("saved", 0) > 7 * 86400:
            return
        self.messages = msgs
        results_by_id = {}
        for m in msgs:
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    results_by_id[b.get("tool_use_id")] = c if isinstance(c, str) else "\n".join(
                        x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
        for m in msgs:
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if m["role"] == "user" and b.get("type") == "text" and not b["text"].startswith("[automatic"):
                    self.board.chat_add("user", re.sub(r"^\[[^\]]{8,30}\] ", "", b["text"]), status="delivered")
                elif m["role"] == "assistant" and b.get("type") == "text" and b.get("text", "").strip():
                    self.board.chat_add("agent", b["text"])
                elif m["role"] == "assistant" and b.get("type") == "tool_use":
                    cid = self._chat_tool(b["name"], b.get("input") or {}, "ok")
                    res = results_by_id.get(b.get("id"))
                    if res is not None:
                        self.board.chat_update(cid, result=_short(res.replace("\n", " "), 160), has_detail=True)
                        self.board.chat_detail(cid, result=res)
        self.board.chat_add("system", "(conversation restored after SDR Pilot restarted)")

    def _chat_tool(self, name: str, args: dict, status: str) -> int:
        """A tool call in the dashboard chat: the classic one-line text plus what the card view needs (friendly title,
        a short argument hint); the full arguments are kept for the expand button."""
        try:
            from . import server as _srv
            title = _srv.TOOL_TITLES.get(name) or name
        except Exception:  # pragma: no cover
            title = name
        hint = ", ".join(f"{k}={_short(v, 40)}" for k, v in args.items()
                         if v not in (None, "", [], {}) and k not in ("code", "blocks", "connections", "_source_code"))
        if isinstance(args.get("blocks"), list):
            hint = (hint + ", " if hint else "") + f"{len(args['blocks'])} blocks"
        cid = self.board.chat_add("tool", f"{name}({_short(args, 80)})", status=status, tool=name, title=title,
                                  hint=_short(hint, 110))
        try:
            pretty = json.dumps(args, indent=2, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover
            pretty = str(args)
        self.board.chat_detail(cid, tool=name, args=pretty)
        return cid

    # ------------------------------------------------------------------ settings
    def _settings(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def conf(self) -> dict:
        """Effective provider settings: sdrpilot_config.json agent_* keys win over what the dashboard saved."""
        s = self._settings()
        preset = s.get("preset") or ("custom" if self.cfg.agent_base_url else "anthropic")
        if self.cfg.agent_provider and not s.get("preset"):
            preset = "anthropic" if self.cfg.agent_provider == "anthropic" else "custom"
        p = PRESETS.get(preset, PRESETS["custom"])
        base = self.cfg.agent_base_url or s.get("base_url") or p["base_url"]
        key = (self.cfg.agent_api_key or (s.get("keys") or {}).get(preset, "")
               or (os.environ.get(p["key_env"], "") if p.get("key_env") else ""))
        model = self.cfg.agent_model or s.get("model") or (p["models"][0] if p["models"] else "")
        # images to the model: "on" / "off", or "auto" = what the provider says about the model (LM Studio and
        # Ollama report vision models; cloud presets are vision-capable)
        vmode = s.get("vision_mode") or ({True: "on", False: "off"}.get(s.get("vision")) if "vision" in s else "auto")
        detected = self._vision_auto.get((base, model))
        if vmode == "auto":
            vision = detected if detected is not None else p["vision"]
        else:
            vision = vmode == "on"
        return {"preset": preset, "provider": self.cfg.agent_provider or p["provider"], "base_url": base, "key": key,
                "model": model, "vision": bool(vision), "vision_mode": vmode, "vision_detected": detected,
                "needs_key": p["needs_key"], "label": p["label"],
                **{k: s.get(k, v) for k, v in DEFAULT_SETTINGS.items()}}

    def configure(self, preset: str | None = None, base_url: str | None = None, model: str | None = None,
                  api_key: str | None = None, vision: bool | None = None, context_tokens=None, vision_mode=None,
                  tool_detail: str | None = None, compact_history=None, nudge=None, stream=None,
                  auto_recover=None, max_reply_tokens=None, custom_instructions: str | None = None,
                  show_reasoning=None) -> dict:
        s = self._settings()
        if preset:
            if preset not in PRESETS:
                raise ValueError(f"unknown provider preset {preset!r}; one of {sorted(PRESETS)}")
            if preset != s.get("preset"):
                s.pop("model", None)
                s.pop("base_url", None)
                s.pop("vision", None)
            s["preset"] = preset
        cur = s.get("preset") or "anthropic"
        if base_url is not None:
            s["base_url"] = base_url.strip().rstrip("/")
        if model is not None:
            s["model"] = model.strip()
        if vision is not None:
            s["vision"] = bool(vision)
        if vision_mode in ("auto", "on", "off"):
            s["vision_mode"] = vision_mode
            s.pop("vision", None)
        if api_key is not None:
            s.setdefault("keys", {})[cur] = api_key.strip()
        if context_tokens not in (None, ""):
            s["context_tokens"] = max(0, int(float(context_tokens)))
        if tool_detail in ("auto", "full", "short"):
            s["tool_detail"] = tool_detail
        if compact_history is not None:
            s["compact_history"] = bool(compact_history)
        if nudge is not None:
            s["nudge"] = bool(nudge)
        if stream is not None:
            s["stream"] = bool(stream)
        if auto_recover is not None:
            s["auto_recover"] = bool(auto_recover)
        if max_reply_tokens not in (None, ""):
            s["max_reply_tokens"] = max(1024, min(int(float(max_reply_tokens)), MAX_REPLY_CAP))
        if show_reasoning is not None:
            s["show_reasoning"] = bool(show_reasoning)
        if custom_instructions is not None:
            s["custom_instructions"] = str(custom_instructions).strip()[:6000]
        self._budget_cache = None
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, self.path)
        self._tools = None
        self.publish()
        return self.state()

    def state(self) -> dict:
        c = self.conf()
        k = c["key"]
        missing = [] if c["model"] and c["base_url"] else ["model" if not c["model"] else "base_url"]
        if c["needs_key"] and not k:
            missing.append("api_key")
        return {"configured": not missing, "missing": missing, "preset": c["preset"], "label": c["label"],
                "provider": c["provider"], "base_url": c["base_url"], "model": c["model"], "vision": c["vision"],
                "vision_mode": c["vision_mode"], "vision_detected": c["vision_detected"],
                "key_hint": (k[:6] + "…" + k[-4:]) if len(k) > 14 else ("set" if k else ""),
                "presets": {n: {kk: p[kk] for kk in ("label", "provider", "base_url", "models", "needs_key")}
                            for n, p in PRESETS.items()},
                "busy": self.busy, "queued": len(self.queue), "usage": dict(self.usage),
                "context_tokens": self.last_context,
                "settings": {k: c[k] for k in DEFAULT_SETTINGS},
                "budget": self._budget_cache[1] if self._budget_cache else None,
                "budget_source": self._budget_cache[2] if self._budget_cache else "",
                "tools_short": self._short_tools(c) if self._budget_cache else None,
                "compacted": self.compacted,
                "infer": dict(self.infer) if self.infer else None, "last_call": self.last_call}

    def publish(self) -> None:
        self.board.set_chat_state(agent=self.state())

    def list_models(self) -> list[str]:
        return [m["id"] for m in self.list_models_info()]

    def list_models_info(self) -> list[dict]:
        """Every model the provider offers. LM Studio's /v1/models can list only the LOADED model(s), so its own
        REST API (/api/v1/models, or /api/v0 before LM Studio 0.4: everything downloaded, loaded or not) and Ollama's /api/tags are asked too."""
        c = self.conf()
        found: dict[str, dict] = {}
        err = None
        if c["provider"] == "anthropic":
            url, headers = c["base_url"].rstrip("/") + "/v1/models?limit=1000", self._anthropic_headers(c)
        else:
            url, headers = c["base_url"].rstrip("/") + "/models", self._openai_headers(c)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                d = json.loads(r.read())
            for m in d.get("data", []) or d.get("models", []) or []:
                if isinstance(m, dict) and (m.get("id") or m.get("name")):
                    mid = str(m.get("id") or m.get("name")).removeprefix("models/")
                    found[mid] = {"id": mid}
        except urllib.error.HTTPError as e:
            err = AgentError(f"{c['label']} refused the model list ({e.code}) - check the API key")
        except urllib.error.URLError as e:
            err = AgentError(f"can't reach {c['base_url']}: {e.reason}")
        except ValueError:
            pass
        if c["provider"] != "anthropic":
            root = re.sub(r"/v1/?$", "", c["base_url"].rstrip("/"))
            lms = lmstudio_models(root, self._openai_headers(c))
            for m in lms:
                if m["type"] not in ("llm", "vlm"):
                    continue
                tags = ["loaded" if m["loaded"] else "not loaded"]
                if m["vision"]:
                    tags.append("vision")
                if m["max_ctx"]:
                    tags.append(f"{int(m['max_ctx']) // 1024}k ctx")
                if m["quant"]:
                    tags.append(str(m["quant"]))
                found[m["id"]] = {"id": m["id"], "info": " · ".join(tags), "loaded": m["loaded"]}
            if lms:
                err = None
            try:  # Ollama: every pulled model
                with urllib.request.urlopen(f"{root}/api/tags", timeout=6) as r:
                    d = json.loads(r.read())
                for m in d.get("models", []):
                    if isinstance(m, dict) and m.get("name"):
                        det = m.get("details") or {}
                        found.setdefault(m["name"], {"id": m["name"]})["info"] = " · ".join(
                            x for x in (det.get("parameter_size"), det.get("quantization_level")) if x)
                err = None
            except Exception:
                pass
        if not found and err:
            raise err
        embed = re.compile(r"embed|bge-|e5-|minilm|rerank", re.I)
        return sorted((m for m in found.values() if not embed.search(m["id"])),
                      key=lambda m: (not m.get("loaded"), m["id"].lower()))

    # ------------------------------------------------------------------ control
    def submit(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        mid = self.board.chat_add("user", text, status="queued")
        st = self.state()
        if not st["configured"]:
            self.board.chat_update(mid, status="error")
            self.board.chat_add("system", "Set up the dashboard agent first: open Settings (⚙), pick a model provider "
                                          f"(Claude, OpenAI, Gemini, OpenRouter or a local LM Studio / Ollama model) - "
                                          f"missing: {', '.join(st['missing'])}.")
            return {"id": mid, "error": "agent not configured"}
        with self.lock:
            self.queue.append((mid, text))
            if not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self._work, daemon=True, name="sdrpilot-agent")
                self.worker.start()
        self.last_active = time.time()
        self.publish()
        return {"id": mid}

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            dropped, self.queue = self.queue, []
        for mid, _ in dropped:
            self.board.chat_update(mid, status="cancelled")
        self.publish()

    def request_report(self, text: str) -> dict:
        """'📋 report' from the dashboard. While the agent works it interrupts at the next step boundary: the model
        must answer with a short status report (tools disabled for that one reply), then carries on by itself.
        When idle it's simply a question."""
        if self.busy:
            self.report_pending = True
            mid = self.board.chat_add("user", "📋 Status report, please.", status="queued")
            self._report_mid = mid
            self.board.chat_add("system", "(the AI will stop and report after the tool call it's running now)")
            self.publish()
            return {"id": mid, "interrupt": True}
        return self.submit(text)

    def reset(self) -> None:
        """New chat = a brand-new conversation: nothing from the old one is sent to the model again."""
        self.stop()
        if self.worker:
            self.worker.join(timeout=30)
        self.messages = []
        self.last_context = 0
        self.compacted = 0
        self.report_pending = False
        self._no_tools_once = False
        self._warned_small = False
        self._prev_req = ""
        self.last_call = None
        self.infer = None
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
        self._save()
        if self.results is not None:  # full copies of the old conversation's shortened results
            try:
                import shutil
                shutil.rmtree(self.results.folder, ignore_errors=True)
                self.results.retag(os.path.basename(self.results.folder))
            except Exception:  # pragma: no cover
                pass
        if callable(self.on_reset):
            try:
                self.on_reset()
            except Exception:  # pragma: no cover
                log.exception("on_reset failed")
        self.board.chat_clear()
        self.board.chat_add("system", "New chat - the AI starts with a fresh, empty context.")
        self.publish()

    # ------------------------------------------------------------------ worker
    def _work(self) -> None:
        while True:
            with self.lock:
                items, self.queue = self.queue, []
                if not items:
                    self.busy = False
                    self.worker = None
                    self.publish()
                    return
                self.busy = True
            self.publish()
            try:
                asyncio.run(self._turn(items))
                if self.report_pending:  # the turn ended before a step boundary: ask it as a normal message
                    self.report_pending = False
                    with self.lock:
                        self.queue.append((getattr(self, "_report_mid", None) or self.board.chat_add(
                            "user", "📋 Status report, please.", status="queued"), REPORT_TEXT))
            except AgentError as e:
                self.board.chat_add("system", str(e))
            except Exception as e:  # pragma: no cover
                log.exception("agent turn failed")
                self.board.chat_add("system", f"Agent error: {type(e).__name__}: {e}")
            self.last_active = time.time()

    # ------------------------------------------------------------------ context budget
    @staticmethod
    def _is_local(c: dict) -> bool:
        return c["preset"] in ("lmstudio", "ollama") or any(h in c["base_url"] for h in LOCAL_HOSTS)

    def budget(self) -> tuple[int, str]:
        """How many tokens the model can take: the user's setting, else what a local server reports, else a default."""
        c = self.conf()
        if int(c.get("context_tokens") or 0) > 0:
            if self._is_local(c) and (c["base_url"], c["model"]) not in self._vision_auto:
                self._detect_context(c)  # still find out whether it's a vision model
            return int(c["context_tokens"]), "set in Settings"
        key = (c["base_url"], c["model"])
        bc = self._budget_cache
        if bc and bc[0] == key and time.time() - bc[3] < 120:
            return bc[1], bc[2]
        tokens, src = (None, "")
        if self._is_local(c):
            tokens, src = self._detect_context(c)
            if not tokens:
                tokens, src = 32000, "default for local models"
        else:
            tokens, src = (180_000, "default") if c["provider"] == "anthropic" else (120_000, "default")
        self._budget_cache = (key, tokens, src, time.time())
        return tokens, src

    def _detect_context(self, c: dict) -> tuple[int | None, str]:
        root = re.sub(r"/v1/?$", "", c["base_url"].rstrip("/"))
        try:  # LM Studio REST API: the context length the model is loaded with
            m = next((x for x in lmstudio_models(root, self._openai_headers(c)) if x["id"] == c["model"]
                      or c["model"] in x["instances"]), None)
            if m:
                self._vision_auto[(c["base_url"], c["model"])] = m["vision"]
                n = m["loaded_ctx"] or m["max_ctx"]
                if n:
                    return int(n), "reported by LM Studio" + ("" if m["loaded_ctx"] else " (model max)")
        except Exception:
            pass
        try:  # Ollama
            req = urllib.request.Request(f"{root}/api/show", data=json.dumps({"model": c["model"]}).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=4) as r:
                d = json.loads(r.read())
            if isinstance(d.get("capabilities"), list):
                self._vision_auto[(c["base_url"], c["model"])] = "vision" in d["capabilities"]
            for k, v in (d.get("model_info") or {}).items():
                if k.endswith("context_length"):
                    return min(int(v), 32768), "reported by Ollama"
        except Exception:
            pass
        return None, ""

    def _short_tools(self, c: dict | None = None) -> bool:
        c = c or self.conf()
        mode = c.get("tool_detail") or "auto"
        if getattr(self, "_force_short", False) and mode != "full":
            return True
        if mode in ("full", "short"):
            return mode == "short"
        tokens = self._budget_cache[1] if self._budget_cache else self.budget()[0]
        return tokens < 64_000

    @staticmethod
    def _shorten_desc(desc: str) -> str:
        first = desc.strip().split("\n\n")[0].replace("GNU Radio (installed on the user's PC). ", "")
        first = " ".join(first.split())
        m = re.search(r"^(.{40,260}?[.!?])(\s|$)", first)
        s = m.group(1) if m else first[:260]
        return s + ("" if len(s) >= len(" ".join(desc.split())) else " (more: tool_help)")

    async def _tool_defs(self) -> list[dict]:
        short = self._short_tools()
        if self._tools is None or self._tools_key != short:
            out = []
            for t in await self.mcp.list_tools():
                d = t.model_dump(by_alias=True, exclude_none=True) if hasattr(t, "model_dump") else dict(t)
                if d["name"] in EXCLUDE:
                    continue
                desc = (d.get("description") or "")[:4000]
                self._tools_full[d["name"]] = desc
                out.append({"name": d["name"], "description": self._shorten_desc(desc) if short else desc,
                            "input_schema": d.get("inputSchema") or d.get("input_schema") or {"type": "object"}})
            if short:
                out.append(dict(TOOL_HELP))
            if out:
                out[-1]["cache_control"] = {"type": "ephemeral"}
            self._tools, self._tools_key = out, short
        return self._tools

    def _take_mid_turn(self) -> str:
        with self.lock:
            items, self.queue = self.queue, []
        for mid, _ in items:
            self.board.chat_update(mid, status="delivered")
        self.publish()
        return "\n\n".join(t for _, t in items)

    async def _turn(self, items: list[tuple[int, str]]) -> None:
        self.cancel.clear()
        for mid, _ in items:
            self.board.chat_update(mid, status="delivered")
        stamp = time.strftime("%a %Y-%m-%d %H:%M")
        self.messages.append({"role": "user", "content": [{"type": "text", "text": f"[{stamp}] " + "\n\n".join(t for _, t in items)}]})
        budget, _src = self.budget()
        tools = await self._tool_defs()
        ci = str(self.conf().get("custom_instructions") or "").strip()
        own = ("\n\nTHE USER'S OWN INSTRUCTIONS (their station and preferences - follow them unless they conflict "
               "with safety or SDR Pilot's receive-only rule):\n" + ci) if ci else ""
        system = [{"type": "text", "text": self.system_prompt + AGENT_PROMPT + own, "cache_control": {"type": "ephemeral"}}]
        fixed = (len(system[0]["text"]) + len(json.dumps(tools))) / CHARS_PER_TOKEN
        if fixed > 0.6 * budget and not self._warned_small:
            self._warned_small = True
            self.board.chat_add("system", f"This model has a {budget:,}-token context ({_src}), but SDR Pilot's instructions "
                                          f"and tools alone take ~{int(fixed):,}. Load the model with a longer context "
                                          "(LM Studio: Context Length 32768 or more) or it will forget things.")
        stalls = 0  # consecutive replies that announced a step without making the call
        turn_tools: list[tuple[str, bool]] = []  # (tool, failed) for every call this turn - the nudge looks at them
        recoveries = 0
        report_step = False   # this step answers the user's "report" button (tools disabled for it)
        task_touched = False  # the AI updated the task card during this turn
        reminded = False
        self._max_out = int(self.conf().get("max_reply_tokens") or 16384)
        for _step in range(max(1, int(self.cfg.agent_max_steps))):
            if self.cancel.is_set():
                self.board.chat_add("system", "Stopped.")
                return
            self._shrink(budget)
            thinking = self.board.chat_add("status", "thinking…")
            self._live_mid = None
            self._thought_mid = None
            try:
                resp = await asyncio.to_thread(self._complete, system, tools, self._prepared())
            except AgentError as e:
                if re.search(r"max_(completion_)?tokens|max(imum)? output|output tokens", str(e), re.I) and \
                        not CONTEXT_ERR_RE.search(str(e)) and self._max_out > 2048:
                    self._max_out //= 2  # the provider/model allows less output: halve the limit and retry
                    continue
                if not (CONTEXT_ERR_RE.search(str(e)) and self.conf().get("auto_recover", True) and recoveries < 4):
                    raise
                recoveries += 1
                if self._recover_context(recoveries, str(e)):
                    budget = self.budget()[0]
                    tools = await self._tool_defs()
                    continue
                raise AgentError(f"{e} - and there is nothing left to shrink. Load the model with a longer "
                                 "context (LM Studio: Context Length) or start a new chat.")
            finally:
                self.board.chat_update(thinking, role="status", text="", hidden=True)
                self.infer = None
                self.publish()
            content = resp.get("content") or []
            self.messages.append({"role": "assistant", "content": content})
            text = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if self._live_mid is not None:  # the reply was streamed into the chat as it was written
                if text:
                    self.board.chat_update(self._live_mid, text=text, streaming=False)
                else:
                    self.board.chat_update(self._live_mid, hidden=True, streaming=False)
            elif text:
                self.board.chat_add("agent", text)
            uses = [b for b in content if b.get("type") == "tool_use"]
            if report_step and resp.get("stop_reason") != "max_tokens":
                report_step = False
                if uses:  # it should only have reported - don't run what it asked for
                    content = [b for b in content if b.get("type") != "tool_use"] or [
                        {"type": "text", "text": "(no report written)"}]
                    self.messages[-1] = {"role": "assistant", "content": content}
                if not text:
                    self.board.chat_add("system", "(the model didn't write a report - the activity summary above "
                                                  "shows what it has been doing)")
                if text and STOP_RE.search(text[-400:]):
                    self._save()
                    return  # the report says it's blocked / waiting / done: hand back rather than push on
                self.messages.append({"role": "user", "content": [{"type": "text", "text":
                    "[automatic note from SDR Pilot] Thanks - the user has your report. Carry on with the task where "
                    "you left off if there's a sensible next step; if you're blocked or done, say so and stop."}]})
                self._save()
                continue
            if resp.get("stop_reason") != "tool_use" or not uses:
                if resp.get("stop_reason") == "max_tokens":  # finish_reason "length"
                    if self.conf().get("auto_recover", True) and recoveries < 4:
                        recoveries += 1
                        self.messages.pop()  # drop the cut-off reply; it is regenerated
                        if self._live_mid is not None:
                            self.board.chat_update(self._live_mid, hidden=True)
                        out = resp.get("out_tokens") or 0
                        # the context filled up: a short reply, a prompt already near the limit, or prompt + reply
                        # reaching the model's context
                        ctx_full = (out < 3000 or self.last_context >= 0.85 * budget
                                    or self.last_context + out >= 0.95 * budget)
                        if ctx_full and self._recover_context(recoveries, "", cut_off=True):
                            budget = self.budget()[0]
                            tools = await self._tool_defs()
                            continue
                        raw = resp.get("raw_out") or ""
                        looping = _is_repetitive(raw)
                        room = int(budget - self.last_context - 1024)
                        new_max = min(self._max_out * 2, MAX_REPLY_CAP, max(room, 0))
                        if not ctx_full and not looping and new_max > self._max_out:
                            # a long reply that's genuinely needed (e.g. a big flowgraph in one call): give it room
                            # and regenerate rather than asking for something shorter
                            self.board.chat_add("system", f"(the reply needed more than {self._max_out:,} tokens - "
                                                          f"allowed up to {new_max:,} and regenerated it)")
                            self._max_out = new_max
                            continue
                        why = ("it was repeating itself" if looping else
                               "it didn't fit in the space left in the model's context")
                        self.board.chat_add("system", f"(the reply was cut off - {why} - asked the model to carry on "
                                                      "in smaller pieces)")
                        self.messages.append({"role": "user", "content": [{"type": "text", "text":
                            "[automatic note from SDR Pilot] Your last reply was cut off at the length limit ("
                            + why + ") and was discarded. " + ("Don't repeat yourself; make the next tool call now. "
                            if looping else "Do it in smaller pieces: build a large flowgraph block by block with "
                            "gnuradio_add_block, or build a first part and extend it with gnuradio_edit_flowgraph; "
                            "split long text across replies.")}]})
                        continue
                    self.board.chat_add("system", "(reply cut off at the length limit)")
                    self._save()
                    return
                nudge, why_not = (self._should_nudge(text, turn_tools) if self.conf().get("nudge", True) and stalls < 1
                                  else (False, ""))
                if nudge:
                    stalls += 1
                    self.board.chat_add("system", "(the model said what it would do next without doing it - "
                                                  "reminded it once)")
                    said = " ".join(text.split())[-160:]
                    self.messages.append({"role": "user", "content": [{"type": "text", "text":
                        f"[automatic note from SDR Pilot] You wrote \"…{said}\" but made no tool call, so nothing "
                        "happened. If you meant to do that now, make the tool call (tool-call format, not text). If "
                        "you've actually reached a point where you can't or shouldn't continue - blocked, waiting on "
                        "the user, nothing useful left to try - that's fine: just say so in one or two sentences and "
                        "stop. Don't invent busywork."}]})
                    self._save()
                    continue
                if "failed" in why_not and stalls == 0:
                    self.board.chat_add("system", f"(not nudged: {why_not})")
                if not reminded and not task_touched and self._task_open():
                    reminded = True  # after a written reply: bring the task card in line with it (once per turn)
                    self.messages.append({"role": "user", "content": [{"type": "text", "text":
                        "[automatic note from SDR Pilot] Before handing back to the user: update the task card to "
                        "match what you just said - task_step (mark steps done / active / failed, insert new ones), "
                        "task_finish if the task is complete, or task_finish(success=false) with the reason if you're "
                        "blocked. Only task_* calls now, no new work, no repeat of your message."}]})
                    self._save()
                    continue
                self._save()
                return
            stalls = 0
            if any(b.get("name", "").startswith("task_") or (b.get("name") == "batch" and "task_" in json.dumps(
                    b.get("input") or {}, default=str)) for b in uses):
                task_touched = True   # the card reflects the work so far...
            elif uses:
                task_touched = False  # ...until more work happens after the last update
            results = []
            for b in uses:
                if self.cancel.is_set():
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                    "content": [{"type": "text", "text": "cancelled by the user"}]})
                    continue
                cid = self._chat_tool(b["name"], b.get("input") or {}, "running")
                blocks, is_err = await self._run_tool(b["name"], b.get("input") or {})
                turn_tools.append((b["name"], bool(is_err) or self._looks_failed(blocks)))
                blocks = self._fit_result(b["name"], blocks, budget)
                full = "\n".join(x.get("text", "") for x in blocks if x["type"] == "text")
                self.board.chat_update(cid, status="error" if is_err else "ok", result=_short(full.replace("\n", " "), 160),
                                       has_detail=True)
                self.board.chat_detail(cid, result=full, images=sum(1 for x in blocks if x.get("type") == "image"))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": blocks, "is_error": is_err})
            extra = self._take_mid_turn()
            if extra:
                results.append({"type": "text", "text": f"[The user just sent this from the dashboard]: {extra}"})
            if self.report_pending:
                self.report_pending = False
                report_step = True
                self._no_tools_once = True
                if getattr(self, "_report_mid", None) is not None:
                    self.board.chat_update(self._report_mid, status="delivered")
                results.append({"type": "text", "text":
                    "[STATUS REPORT REQUESTED by the user - answer this first] Pause and tell the user, in 2-4 "
                    "plain sentences: what you're working on right now, what you've found or changed so far, and "
                    "what you'll do next. Write the report only - no tool calls in this reply. You'll continue "
                    "afterwards."})
            self.messages.append({"role": "user", "content": results})
            self._save()
            if self.cancel.is_set():
                self.board.chat_add("system", "Stopped.")
                return
        self.board.chat_add("system", f"Stopped after {self.cfg.agent_max_steps} tool steps - say 'continue' to go on.")

    def _task_open(self) -> bool:
        try:
            p = self.prefs.get() if self.prefs is not None else {}
        except Exception:
            p = {}
        if not p.get("task_reminders", True) or not p.get("task_after_reply", True):
            return False
        t = self.board.task or {}
        return bool(t.get("goal")) and not t.get("finished")

    @staticmethod
    def _looks_failed(blocks: list[dict]) -> bool:
        t = " ".join(b.get("text", "")[:600] for b in blocks if b.get("type") == "text")
        return bool(re.search(r'"(error|errors)"\s*:\s*("[^"]|\[\s*")|"ok"\s*:\s*false|"timed_out"\s*:\s*true|^ERROR', t))

    def _should_nudge(self, text: str, turn_tools: list[tuple[str, bool]]) -> tuple[bool, str]:
        """Push the model on only when it announced a concrete step it can do right now and nothing suggests it
        stopped on purpose. Returns (nudge?, why not) - the reason is shown in the chat when it looked like a stall."""
        t = (text or "").strip()
        if not self._announced(t):
            return False, ""
        tail = t[-400:]
        if STOP_RE.search(tail):
            return False, "it says it's blocked, waiting on you or done"
        if any(n in ("user_action", "dashboard_reply") for n, _ in turn_tools[-3:]):
            return False, "it's waiting for you to do something"
        try:
            if any(a.get("status") == "waiting" for a in (self.board.user_actions or [])):
                return False, "a request for you to do something is still open"
        except Exception:
            pass
        recent = turn_tools[-4:]
        if len(recent) >= 3 and all(f for _, f in recent):
            return False, "its last few tool calls all failed - pushing it to retry wouldn't help"
        if len(turn_tools) >= 6 and sum(f for _, f in turn_tools[-6:]) >= 4:
            return False, "most of its recent tool calls failed"
        last = re.split(r"(?<=[.!])\s+", t)[-1] if not t.endswith(":") else t[-200:]
        if not t.endswith(":") and not ACTION_RE.search(last):
            return False, "no concrete next step to take"
        return True, ""

    @staticmethod
    def _announced(text: str) -> bool:
        t = (text or "").strip()
        # judge the last sentence or two only: an intent at the end with no question for the user,
        # or a reply that ends in ":" ("Here is the corrected flowgraph:") - the call it introduces never came
        if not t or t.endswith("?"):
            return False
        return t.endswith(":") or bool(INTENT_RE.search(t[-200:]))

    def _fit_result(self, name: str, blocks: list[dict], budget: int) -> list[dict]:
        """Results are already compact and capped by the server; a small-context model gets a tighter cap
        (the full copy stays available through get_full_result). A model that can't see images doesn't get
        advice about looking at them."""
        if not self.conf()["vision"]:
            out = []
            for b in blocks:
                if b.get("type") == "text" and ("look_at_image" in b["text"] or "vision_hint" in b["text"]):
                    try:
                        d = json.loads(b["text"])
                        if isinstance(d, dict):
                            d.pop("look_at_image", None)
                            d.pop("vision_hint", None)
                            b = {"type": "text", "text": json.dumps(d, separators=(",", ":"), ensure_ascii=False)}
                    except ValueError:
                        pass
                out.append(b)
            blocks = out
        if not (self.results and self.prefs and self.prefs.get().get("shorten_results")) or name == "get_full_result":
            return blocks
        limit = max(2500, int(budget * CHARS_PER_TOKEN * 0.08))
        out = []
        for b in blocks:
            if b.get("type") == "text" and len(b["text"]) > limit:
                b = {"type": "text", "text": self.results.fit(b["text"], name, limit)}
            out.append(b)
        return out

    async def _run_tool(self, name: str, args: dict) -> tuple[list[dict], bool]:
        if name == "tool_help":
            n = str(args.get("name") or "")
            if n not in self._tools_full:
                return [{"type": "text", "text": f"no tool named {n!r}. Tools: {sorted(self._tools_full)}"}], True
            schema = next((t["input_schema"] for t in self._tools or [] if t["name"] == n), {})
            return [{"type": "text", "text": f"{n}: {self._tools_full[n]}\n\nparameters: "
                                             f"{json.dumps(_clean_schema(schema), separators=(',', ':'))}"}], False
        try:
            res = await self.mcp.call_tool(name, args)
        except Exception as e:
            cause = e.__cause__ or e
            return [{"type": "text", "text": f"{type(cause).__name__}: {cause}"}], True
        if isinstance(res, tuple):  # MCP SDK 1.x: (content, structured)
            res = res[0]
        content = getattr(res, "content", res)
        is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
        blocks = []
        for c in content or []:
            t = getattr(c, "type", None)
            if t == "text":
                txt = c.text
                if len(txt) > 60_000:
                    txt = txt[:60_000] + "\n…(truncated)"
                blocks.append({"type": "text", "text": txt})
            elif t == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "data": c.data,
                               "media_type": getattr(c, "mime_type", None) or getattr(c, "mimeType", None) or "image/png"}})
        return blocks or [{"type": "text", "text": "(no output)"}], is_err

    # ------------------------------------------------------------------ history
    def _prepared(self) -> list[dict]:
        msgs = copy.deepcopy(self.messages)
        if not self.conf()["vision"]:  # images never reach a non-vision model; leave history byte-identical
            last = msgs[-1]["content"]
            if isinstance(last, list) and last:
                last[-1] = {**last[-1], "cache_control": {"type": "ephemeral"}}
            return msgs
        # older images are dropped in batches (see _prune_images), never as a rolling window: replacing the oldest
        # image every time a new one arrives would change the start of the prompt each step and defeat the cache
        last = msgs[-1]["content"]
        if isinstance(last, list) and last:
            last[-1] = {**last[-1], "cache_control": {"type": "ephemeral"}}
        return msgs

    def _prune_images(self, keep: int = KEEP_IMAGES, slack: int = 6) -> int:
        """Drop all but the newest `keep` tool images - only once there are `keep + slack` of them, so the prompt
        prefix changes rarely (each change makes a local model re-read everything after it)."""
        imgs = []
        for m in self.messages:
            if not isinstance(m["content"], list):
                continue
            for b in m["content"]:
                inner = b.get("content") if b.get("type") == "tool_result" else None
                if isinstance(inner, list):
                    imgs += [(inner, i) for i, x in enumerate(inner) if x.get("type") == "image"]
        if len(imgs) < keep + slack:
            return 0
        drop = imgs[:len(imgs) - keep]
        for inner, i in drop:
            inner[i] = {"type": "text", "text": "[older image omitted]"}
        return len(drop)

    def _msg_chars(self) -> int:
        return len(json.dumps(self.messages, ensure_ascii=False, default=str))

    def _shrink(self, budget: int) -> None:
        """Keep the prompt within the context - in ONE big step, rarely. Local servers (LM Studio / llama.cpp) only
        reuse their cache for an unchanged start of the prompt: any edit to old messages makes them re-read everything
        after it. So nothing old is touched until the context is ~85% full; then older tool results are compacted
        AND the oldest exchanges moved out together, down to about half the context, which leaves room for many
        steps that reuse the cache before this has to happen again."""
        c = self.conf()
        trim_at = float(c.get("shrink_at") or 0.85)
        target = float(c.get("shrink_to") or 0.5)
        if self.conf()["vision"] and self._prune_images():
            self.last_context = int(self.last_context * 0.9)
        if not self.last_context or self.last_context < trim_at * budget:
            return
        before_chars = max(1, self._msg_chars())
        before_ctx = self.last_context
        done = []
        if c.get("compact_history", True):
            k = self._compact_history(budget, force=True, keep=2, quiet=True)
            if k:
                done.append(f"compacted {k} older tool result(s)")
        for _ in range(40):
            est = before_ctx * self._msg_chars() / before_chars
            if est <= target * budget:
                break
            t = self._trim_history(budget, force=True, quiet=True, min_keep_steps=6)
            if not t:
                break
            if t not in done:
                done.append(t)
        if not done:
            return
        est = int(before_ctx * self._msg_chars() / before_chars)
        self.last_context = est
        self.board.chat_add("system", f"(context {before_ctx * 100 // max(1, budget)}% full - {'; '.join(done)} in one "
                                      f"go, down to ~{est * 100 // max(1, budget)}%. The model reads the shortened "
                                      "prompt once, then reuses its cache again. Full copies kept - the AI can reread "
                                      "them.)")
        self._save()
        self.publish()

    def _recover_context(self, level: int, err: str, cut_off: bool = False) -> bool:
        """The model ran out of context: learn its real size from the error, shrink the prompt (more each time)
        and let the turn carry on. Everything removed from the prompt stays readable via get_full_result."""
        c = self.conf()
        n = _context_limit_from(err) if err else None
        if n and not int(c.get("context_tokens") or 0):
            self._budget_cache = ((c["base_url"], c["model"]), n, "learned from the model's error", time.time())
        budget = self.budget()[0]
        done = []
        k = self._compact_history(budget, force=True, keep=1 if level == 1 else 0)
        if k:
            done.append(f"compacted {k} tool result(s)")
        if level >= 2 or not k:
            t = self._trim_history(budget, force=True)
            if t:
                done.append(t)
        if level >= 2 and not self._short_tools(c) and c.get("tool_detail") != "full":
            self._force_short = True
            self._tools = None
            done.append("switched to short tool descriptions")
        if level >= 3:
            k = self._squeeze_results(1500)
            if k:
                done.append(f"shortened {k} more result(s)")
        if not done:
            return False
        why = "the reply was cut off because the context filled up" if cut_off else "the prompt no longer fit the model's context"
        self.board.chat_add("system", f"({why} - {', '.join(done)}; full copies kept. Continuing.)")
        self.last_context = 0
        self._save()
        self.publish()
        return True

    def _squeeze_results(self, limit: int) -> int:
        if self.results is None:
            return 0
        n = 0
        for m in self.messages:
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for b in m["content"]:
                if b.get("type") != "tool_result":
                    continue
                for i, p in enumerate(b.get("content") or []):
                    if p.get("type") == "text" and len(p["text"]) > limit:
                        b["content"][i] = {"type": "text", "text": self.results.fit(p["text"], "tool", limit)}
                        n += 1
        return n

    def _compact_history(self, budget: int, force: bool = False, keep: int = 1, quiet: bool = False) -> int:
        """When the context is ~2/3 full, replace older tool results with a one-line stub each. The full results are
        saved (get_full_result), so nothing is lost - the AI can reread any of them. Done in one go rather than every
        step, so the prompt stays the same between steps and local servers can reuse their cache.
        Switch: Settings -> Dashboard agent -> compact older tool results."""
        if self.results is None or (not force and (not self.conf().get("compact_history", True)
                                                    or self.last_context < 0.65 * budget)):
            return 0
        tool_msgs = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m["content"], list)
                     and any(b.get("type") == "tool_result" for b in m["content"])]
        keep = set(tool_msgs[-keep:]) if keep else set()  # the newest result stays; compacting deeply = rarely
        n = 0
        for i in tool_msgs:
            if i in keep:
                continue
            for b in self.messages[i]["content"]:
                if b.get("type") != "tool_result" or b.get("compacted"):
                    continue
                txt = "\n".join(p.get("text", "") for p in b.get("content") or [] if p.get("type") == "text")
                if len(txt) < (300 if force else 600):
                    continue
                name = next((u.get("name") for m in self.messages[max(0, i - 1):i] for u in (m["content"] or [])
                             if isinstance(u, dict) and u.get("id") == b.get("tool_use_id")), "") or "tool"
                rid = self.results.put(txt, name)
                b["content"] = [{"type": "text", "text": " ".join(txt[:220].split()) +
                                 f" …[older result compacted to save context; full copy: get_full_result(id='{rid}')]"}]
                b["compacted"] = True
                n += 1
        if n:
            self.compacted += n
            self.last_context = int(self.last_context * 0.6)  # re-measured on the next call
            if not force and not quiet:
                self.board.chat_add("system", f"(compacted {n} older tool result(s) to keep the prompt small - full "
                                              "copies kept, the AI can reread them)")
            self.publish()
        return n

    def _trim_history(self, budget: int, force: bool = False, quiet: bool = False, min_keep_steps: int = 4) -> str:
        """Last resort when the context is nearly full: move the oldest whole exchanges out of the prompt. They're
        saved as one stored result the AI can read back. Within one long task (a single request), the oldest half of
        its tool steps are moved out instead."""
        if not force and self.last_context < 0.85 * budget:
            return ""
        # where the user's own requests start (SDR Pilot's automatic notes don't count)
        starts = [i for i, m in enumerate(self.messages) if m["role"] == "user" and any(
            b.get("type") == "text" and not b.get("text", "").startswith("[automatic note") for b in m["content"])
            and not any(b.get("type") == "tool_result" for b in m["content"])]
        if force and len(starts) < 2:
            first = starts[0] if starts else 0
            steps = [i for i in range(first + 1, len(self.messages)) if self.messages[i]["role"] == "assistant"]
            if len(steps) < max(4, min_keep_steps):
                return ""
            cut = steps[min(len(steps) // 2, len(steps) - min_keep_steps)]
            if cut <= first + 1:
                return ""
            dropped = self.messages[first + 1:cut]
            del self.messages[first + 1:cut]
            rid = self.results.put(json.dumps(dropped, ensure_ascii=False, default=str), "earlier_steps") if self.results else "?"
            self.messages[first]["content"].append({"type": "text", "text":
                f"[{len(dropped) // 2} earlier tool steps of this task were moved out of the prompt to fit the context: "
                f"get_full_result(id='{rid}') shows them]"})
            self.last_context = 0
            return f"moved {len(dropped) // 2} earlier tool steps out of the prompt"
        if len(starts) >= 2:
            cut = starts[1] if len(starts) > 2 else starts[-1]
            if cut > 0:
                dropped = self.messages[:cut]
                del self.messages[:cut]
                note = "(moved the oldest part of the conversation out of the prompt to stay within limits)"
                if self.results is not None:
                    rid = self.results.put(json.dumps(dropped, ensure_ascii=False, default=str), "earlier_conversation")
                    self.messages[0]["content"].insert(0, {"type": "text", "text":
                        f"[Earlier conversation moved out of the prompt to save context: get_full_result(id='{rid}')]"})
                    note = note[:-1] + f" - saved, the AI can reread it)"
                if not force and not quiet:
                    self.board.chat_add("system", note)
                self.last_context = 0
                return "moved the oldest part of the conversation out of the prompt"
        return ""

    # ------------------------------------------------------------------ API
    # Conversation history is kept in Anthropic's block format; _openai_* convert to and from Chat Completions.
    def _complete(self, system: list[dict], tools: list[dict], msgs: list[dict]) -> dict:
        c = self.conf()
        no_tools, self._no_tools_once = self._no_tools_once, False
        if c["provider"] == "anthropic":
            body = {"model": c["model"], "max_tokens": self._max_out, "system": system, "tools": tools, "messages": msgs}
            if no_tools:
                body["tool_choice"] = {"type": "none"}
            self._begin_infer(c, {"messages": msgs, "tools": tools})
            resp = self._post(c, c["base_url"].rstrip("/") + "/v1/messages", body, self._anthropic_headers(c))
            u = resp.get("usage") or {}
            self._count(u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens") or 0,
                        u.get("cache_creation_input_tokens") or 0)
            resp["out_tokens"] = int(u.get("output_tokens") or 0)
            resp["raw_out"] = " ".join(b.get("text", "") if b.get("type") == "text" else json.dumps(b.get("input") or {})
                                       for b in resp.get("content") or [])
            return resp
        body = {"model": c["model"], "messages": self._openai_messages(system, msgs, c["vision"]),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": _clean_schema(t["input_schema"])}}
                          for t in tools]}
        body["max_completion_tokens" if "api.openai.com" in c["base_url"] else "max_tokens"] = self._max_out
        if no_tools:
            body["tool_choice"] = "none"
        url = c["base_url"].rstrip("/") + "/chat/completions"
        if c.get("stream", True) and not self._no_stream:
            try:
                resp = self._stream(c, url, body)
            except _StreamUnsupported as e:
                log.info("streaming not usable with %s (%s) - using plain requests", c["base_url"], e)
                self._no_stream = True
                resp = self._post(c, url, body, self._openai_headers(c))
        else:
            self._begin_infer(c, body)
            resp = self._post(c, url, body, self._openai_headers(c))
        u = resp.get("usage") or {}
        self._count(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0, 0)
        ch = (resp.get("choices") or [{}])[0]
        m = ch.get("message") or {}
        content = []
        if isinstance(m.get("content"), str):   # plain (non-streamed) replies: reasoning written into the reply
            th, vis = _split_think(m["content"])
            if th:
                m["content"] = vis
            rs = str(m.get("reasoning_content") or m.get("reasoning") or "") + th
            if rs.strip() and getattr(self, "_thought_mid", None) is None:
                self._thought(rs, done=True)
        if m.get("content"):
            txt = m["content"] if isinstance(m["content"], str) else " ".join(
                p.get("text", "") for p in m["content"] if isinstance(p, dict))
            if txt.strip():
                content.append({"type": "text", "text": txt})
        if not no_tools and not m.get("tool_calls") and content and content[0]["type"] == "text":
            calls, rest = _text_tool_calls(content[0]["text"], {t["name"] for t in tools})
            if calls:  # the model wrote its tool call as text and the server didn't recognise it
                m["tool_calls"] = calls
                content = [{"type": "text", "text": rest}] if rest.strip() else []
        for i, tc in enumerate(m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            content.append({"type": "tool_use", "id": tc.get("id") or f"call_{time.time_ns()}_{i}",
                            "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {}})
        fr = ch.get("finish_reason")
        # a "length" stop means the reply - possibly a half-written tool call - was cut off: never run that
        stop = "max_tokens" if fr == "length" else "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        raw_out = (m.get("content") if isinstance(m.get("content"), str) else "") or ""
        raw_out += "".join(str((tc.get("function") or {}).get("arguments") or "") for tc in m.get("tool_calls") or [])
        return {"content": content, "stop_reason": stop, "out_tokens": int(u.get("completion_tokens") or 0),
                "raw_out": raw_out}

    @staticmethod
    def _openai_messages(system: list[dict], msgs: list[dict], vision: bool) -> list[dict]:
        out = [{"role": "system", "content": "\n\n".join(b["text"] for b in system)}]
        for m in msgs:
            blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": str(m["content"])}]
            if m["role"] == "assistant":
                txt = "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                         for b in blocks if b.get("type") == "tool_use"]
                msg = {"role": "assistant", "content": txt or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
                continue
            images, texts = [], []
            for b in blocks:
                if b.get("type") == "tool_result":
                    parts = b.get("content") or []
                    txt = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
                    imgs = [p for p in parts if p.get("type") == "image"]
                    if imgs and not vision:
                        txt += f"\n[{len(imgs)} image(s) shown to the user on the dashboard; not sent to you]"
                    images += imgs if vision else []
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " if b.get("is_error") else "") + (txt or "(no output)")})
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if images:
                out.append({"role": "user", "content": [{"type": "text", "text": "Images returned by the tools above:"}] + [
                    {"type": "image_url", "image_url": {"url": f"data:{p['source']['media_type']};base64,{p['source']['data']}"}}
                    for p in images]})
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        return out

    # ------------------------------------------------------------------ live inference progress
    def _set_infer(self, force: bool = False, **kw) -> None:
        if self.infer is None:
            return
        self.infer.update(kw)
        now = time.time()
        if force or now - self._pub_t > 0.25:
            self._pub_t = now
            self.infer["elapsed"] = round(now - self.infer["t0"], 2)
            self.publish()

    def _begin_infer(self, c: dict, body: dict) -> None:
        """Estimate how much of this prompt the server must process (the rest is cached from the previous call:
        llama.cpp / LM Studio / Ollama reuse the longest common prefix) and how long that will take."""
        req = json.dumps(body.get("messages"), ensure_ascii=False)
        tools_chars = len(json.dumps(body.get("tools") or []))
        prev = self._prev_req
        common = 0
        if prev:  # longest common prefix, by binary search on C-level slice compares
            lo, hi = 0, min(len(req), len(prev))
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if req[:mid] == prev[:mid]:
                    lo = mid
                else:
                    hi = mid - 1
            common = lo
        self._prev_req = req
        cpt = self._chars_per_token or CHARS_PER_TOKEN
        total = int((len(req) + tools_chars) / cpt)
        new = total if not prev else int((len(req) - common) / cpt)
        expected = (new / self._prompt_tps) if self._prompt_tps else None
        self.infer = {"phase": "prompt", "t0": time.time(), "prompt_tokens": total, "new_tokens": max(1, new),
                      "cached_tokens": max(0, total - new), "expected_s": round(expected, 2) if expected else None,
                      "progress": None, "exact": False, "gen_tokens": 0, "tps": None, "model": c["model"],
                      "local": self._is_local(c), "tool": None}
        self._req_chars = len(req) + tools_chars
        self._set_infer(force=True)
        if c["preset"] == "lmstudio" and self._is_local(c):
            # LM Studio doesn't send llama.cpp's progress over the API, but its server log has it
            if self._lms is None:
                from .lmslog import LMStudioLogTap
                self._lms = LMStudioLogTap()
                import atexit
                atexit.register(self._lms.stop)
            if self._lms.ensure():
                t0 = self.infer["t0"]
                threading.Thread(target=self._watch_lms, args=(t0, c["model"]), daemon=True,
                                 name="sdrpilot-lmsprogress").start()

    def _watch_lms(self, t0: float, model: str) -> None:
        while True:
            inf = self.infer
            if not inf or inf.get("t0") != t0 or inf.get("phase") != "prompt":
                return
            f = self._lms.progress_since(t0, model) if self._lms else None
            if f is not None and (f != inf.get("progress") or not inf.get("exact")):
                self._set_infer(progress=round(f, 3), exact=True)
            time.sleep(0.2)

    def _finish_infer(self, usage: dict, ttft: float | None, gen_s: float | None, gen_tokens: int) -> None:
        inf = self.infer or {}
        lm = self._lms
        if lm and inf.get("t0"):  # LM Studio's log has llama.cpp's exact timings for this call
            time.sleep(0.15)      # the timing lines arrive just after the reply
            pe, ge = lm.prompt_eval, lm.gen_eval
            if pe and pe[0] >= inf["t0"] and pe[2] > 0:
                inf["new_tokens"] = pe[2]
                ttft = pe[1] / 1000.0
            if ge and ge[0] >= inf["t0"] and ge[2] > 0:
                gen_tokens, gen_s = ge[2], ge[1] / 1000.0
                usage = dict(usage, completion_tokens=ge[2])
        pt = int(usage.get("prompt_tokens") or 0)
        if pt and getattr(self, "_req_chars", 0):
            self._chars_per_token = max(1.5, min(6.0, self._req_chars / pt))
        new = inf.get("new_tokens") or 0
        if ttft and new > 50 and ttft > 0.05:
            tps = new / ttft
            self._prompt_tps = tps if not self._prompt_tps else 0.6 * self._prompt_tps + 0.4 * tps
        out = int(usage.get("completion_tokens") or gen_tokens or 0)
        self.last_call = {"t": time.time(), "prompt_tokens": pt or inf.get("prompt_tokens"), "new_tokens": new,
                          "prompt_s": round(ttft, 2) if ttft else None,
                          "prompt_tps": round(new / ttft) if ttft and new else None,
                          "gen_tokens": out, "gen_s": round(gen_s, 2) if gen_s else None,
                          "gen_tps": round(out / gen_s, 1) if gen_s and out else None}
        self._check_speed(out, gen_s, pt)

    def _check_speed(self, out: int, gen_s: float | None, prompt_tokens: int) -> None:
        """A local model whose generation speed collapses (e.g. 25 -> 4 tok/s) has almost always run out of GPU
        memory: its context cache spilled into system RAM. Say so once, with what to do about it."""
        if not out or not gen_s or out < 30 or not self._is_local(self.conf()):
            return
        tps = out / gen_s
        key = str(self.conf().get("model") or "")
        best = getattr(self, "_best_tps", {})
        self._best_tps = best
        b = best.get(key, 0.0)
        if tps > b:
            best[key] = tps
            return
        if b >= 8 and tps < 0.45 * b and time.time() - getattr(self, "_speed_warned", 0) > 1800:
            self._speed_warned = time.time()
            self.board.chat_add("system", (
                f"Generation slowed from {b:.0f} to {tps:.1f} tokens/s with a ~{prompt_tokens:,}-token prompt. That "
                "usually means the model ran out of GPU memory and part of it spilled into system RAM. In LM Studio: "
                "eject and reload the model (clears old conversations it keeps cached), set Max Concurrent "
                "Predictions to 1, or lower the context length / quantize the KV cache. Settings ⚙ → Dashboard "
                "agent → Context size can also cap how long SDR Pilot lets the conversation grow."))

    def _stream(self, c: dict, url: str, body: dict) -> dict:
        """Chat Completions with stream=true: the reply appears on the dashboard as it's written, with prompt
        processing and token-generation progress. Rebuilds the same response shape as a plain request."""
        if c["needs_key"] and not c["key"]:
            raise AgentError(f"No API key for {c['label']} (Settings ⚙).")
        b = dict(body, stream=True, stream_options={"include_usage": True})
        if self._is_local(c) and not self._no_progress_flag:
            b["return_progress"] = True  # llama.cpp server: report prompt-processing progress while streaming
        self._begin_infer(c, body)
        t0 = time.time()
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(b).encode(), method="POST",
                                                              headers=self._openai_headers(c)), timeout=600)
        except urllib.error.HTTPError as e:
            raw = e.read()[:400].decode("utf-8", "replace")
            if e.code == 400 and ("return_progress" in raw or "stream_options" in raw) and not self._no_progress_flag:
                self._no_progress_flag = True
                return self._stream(c, url, body)
            if e.code in (400, 404, 422, 501) and "stream" in raw.lower():
                raise _StreamUnsupported(raw)
            if e.code in (429, 500, 502, 503, 529):
                return self._post(c, url, body, self._openai_headers(c))  # retries with back-off
            try:
                err = json.loads(raw)
                err = err.get("error", err) if isinstance(err, dict) else err
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
            except ValueError:
                msg = raw
            if e.code in (401, 403):
                raise AgentError(f"{c['label']} rejected the API key ({e.code}). Check it in Settings ⚙. {msg}")
            raise AgentError(f"{c['label']} error {e.code}: {msg or e.reason}")
        except urllib.error.URLError:
            return self._post(c, url, body, self._openai_headers(c))
        text, reasoning, calls, usage, finish = [], [], {}, {}, None
        self._thought_mid = None
        self._show_r = bool(self.conf().get("show_reasoning", True))   # read once per reply, not per token
        tk = {"mode": "none", "start": 0, "cut": 0, "th": ""}
        ttft = None
        t_first = None
        ntok = 0
        got_any = False
        with r:
            ctype = r.headers.get("content-type", "")
            if "json" in ctype and "event-stream" not in ctype:  # server ignored stream=true
                return json.loads(r.read())
            for raw in r:
                if self.cancel.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                got_any = True
                if ev.get("error"):
                    err = ev["error"]
                    raise AgentError(f"{c['label']}: {err.get('message', err) if isinstance(err, dict) else err}")
                pp = ev.get("prompt_progress")
                if isinstance(pp, dict) and pp.get("total"):
                    done = (pp.get("processed") or 0)
                    tot = pp["total"]
                    cache = pp.get("cache") or 0
                    frac = max(0.0, min(1.0, (done - cache) / max(1, tot - cache))) if tot > cache else 1.0
                    self._set_infer(progress=round(frac, 3), exact=True, prompt_tokens=tot, cached_tokens=cache,
                                    new_tokens=max(1, tot - cache))
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    piece_r = d.get("reasoning_content") or d.get("reasoning") or ""
                    piece_t = d.get("content") or ""
                    tcs = d.get("tool_calls") or []
                    if (piece_r or piece_t or tcs) and t_first is None:
                        t_first = time.time()
                        ttft = t_first - t0
                    if piece_r:
                        reasoning.append(piece_r)
                        ntok += 1
                        self._set_infer(phase="thinking", gen_tokens=ntok, progress=1.0)
                        if self._thought_due():
                            self._thought("".join(reasoning))
                    if piece_t:
                        text.append(piece_t)
                        ntok += 1
                        joined = "".join(text)
                        # <think>...</think> inside the reply, tracked incrementally (no rescans of the whole text)
                        if tk["mode"] == "none":
                            st = joined.lstrip()
                            if st.startswith("<think>"):
                                tk["mode"], tk["start"] = "open", len(joined) - len(st) + 7
                            elif "</think>" in joined[-(len(piece_t) + 9):]:   # tag opened by the chat template
                                i = joined.find("</think>")
                                tk.update(mode="closed", th=joined[:i], cut=i + 8)
                        elif tk["mode"] == "open" and "</think>" in joined[-(len(piece_t) + 9):]:
                            i = joined.find("</think>", tk["start"])
                            tk.update(mode="closed", th=joined[tk["start"]:i], cut=i + 8)
                        if tk["mode"] == "open":
                            th, vis = joined[tk["start"]:], ""
                        elif tk["mode"] == "closed":
                            th, vis = tk["th"], joined[tk["cut"]:]
                        else:
                            th, vis = "", joined
                        if th and self._thought_due():
                            self._thought("".join(reasoning) + th)
                        if vis.strip():
                            self._live_text(vis)
                            self._set_infer(phase="writing", gen_tokens=ntok, progress=1.0)
                        else:
                            self._set_infer(phase="thinking", gen_tokens=ntok, progress=1.0)
                    for tc in tcs:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": []})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"].append(fn["arguments"])
                        ntok += 1
                        self._set_infer(phase="tool", tool=slot["name"] or None, gen_tokens=ntok, progress=1.0)
                    if t_first:
                        dt = time.time() - t_first
                        if dt > 0.3:
                            self._set_infer(tps=round(ntok / dt, 1))
        if not got_any and not self.cancel.is_set():
            raise _StreamUnsupported("no events in the stream")
        gen_s = (time.time() - t_first) if t_first else None
        if not usage.get("prompt_tokens"):  # server didn't report usage: keep budgeting on the estimate
            usage = {"prompt_tokens": (self.infer or {}).get("prompt_tokens", 0), "completion_tokens": ntok}
        self._finish_infer(usage, ttft, gen_s, ntok)
        th, vis = _split_think("".join(text))
        if reasoning or th:
            self._thought("".join(reasoning) + th, done=True)
        msg = {"role": "assistant", "content": vis or None}
        if calls:
            msg["tool_calls"] = [{"id": s["id"] or f"call_{time.time_ns()}_{i}", "type": "function",
                                  "function": {"name": s["name"], "arguments": "".join(s["args"]) or "{}"}}
                                 for i, s in sorted(calls.items())]
        if self.cancel.is_set() and not finish:
            finish = "stop"
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    def _thought_due(self) -> bool:
        return getattr(self, "_show_r", True) and (getattr(self, "_thought_mid", None) is None
                                                   or time.time() - getattr(self, "_thought_last", 0) > 0.4)

    def _thought(self, text: str, done: bool = False) -> None:
        """The model's reasoning in the dashboard chat as a collapsible 'thinking' bubble, updated as it streams.
        Shown only - it is never sent back to the model."""
        show = getattr(self, "_show_r", None)
        if show is None:
            show = bool(self.conf().get("show_reasoning", True))
        if not text.strip() or not show:
            return
        now = time.time()
        tail = text if len(text) <= THOUGHT_MAX else "…" + text[-THOUGHT_MAX:]
        if getattr(self, "_thought_mid", None) is None:
            self._thought_t0, self._thought_last = now, now
            self._thought_mid = self.board.chat_add("thought", tail, streaming=not done,
                                                    chars=len(text), secs=0.0 if done else None)
            return
        if done or now - getattr(self, "_thought_last", 0) > 0.4:
            self._thought_last = now
            self.board.chat_update(self._thought_mid, text=tail, streaming=not done, chars=len(text),
                                   secs=round(now - self._thought_t0, 1))

    def _live_text(self, text: str) -> None:
        """Show the reply in the dashboard chat while it is being written (tool calls written as text are
        shown once the reply is complete)."""
        if _CALL_MARKERS.search(text) or text.lstrip()[:1] in ("{", "["):
            return
        now = time.time()
        if self._live_mid is None:
            if text.strip():
                self._live_mid = self.board.chat_add("agent", text, streaming=True)
                self._live_t = now
        elif now - getattr(self, "_live_t", 0) > 0.3:
            self._live_t = now
            self.board.chat_update(self._live_mid, text=text)

    @staticmethod
    def _anthropic_headers(c: dict) -> dict:
        return {"x-api-key": c["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}

    @staticmethod
    def _openai_headers(c: dict) -> dict:
        h = {"content-type": "application/json"}
        if c["key"]:
            h["authorization"] = "Bearer " + c["key"]
        if "openrouter.ai" in c["base_url"]:
            h["x-title"] = "SDR Pilot"
        return h

    def _count(self, inp: int, out: int, cache_read: int, cache_write: int) -> None:
        self.usage["input"] += inp
        self.usage["output"] += out
        self.usage["cache_read"] += cache_read
        self.usage["cache_write"] += cache_write
        self.usage["turns"] += 1
        self.last_context = inp + cache_read + cache_write
        self.publish()

    def _post(self, c: dict, url: str, body: dict, headers: dict) -> dict:
        if c["needs_key"] and not c["key"]:
            raise AgentError(f"No API key for {c['label']} (Settings ⚙).")
        data = json.dumps(body).encode()
        delay = 2.0
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    err = json.loads(raw)
                    err = err[0] if isinstance(err, list) and err else err
                    e_obj = err.get("error", err) if isinstance(err, dict) else {}
                    msg = e_obj.get("message", "") if isinstance(e_obj, dict) else str(e_obj)
                except Exception:
                    msg = raw[:300].decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 529) and attempt < 4 and not self.cancel.is_set():
                    wait = float(e.headers.get("retry-after") or delay)
                    self.board.chat_add("system", f"{c['label']} busy ({e.code}), retrying in {wait:.0f} s…")
                    time.sleep(min(wait, 60))
                    delay *= 2
                    continue
                if e.code in (401, 403):
                    raise AgentError(f"{c['label']} rejected the API key ({e.code}). Check it in Settings ⚙. {msg}")
                raise AgentError(f"{c['label']} error {e.code}: {msg or e.reason}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(delay)
                    continue
                local = "127.0.0.1" in url or "localhost" in url
                raise AgentError(f"Can't reach {url}: {e.reason}" + (
                    " - is the local model server running with a model loaded?" if local else ""))
        raise AgentError(f"{c['label']} kept failing; try again later.")


_CALL_MARKERS = re.compile(r"<tool_call>|<\|python_tag\|>|\[TOOL_CALLS\]|<function=|```(?:json|tool_call)?\s*[\[{]", re.I)


def _call_from(o, names: set[str]) -> dict | None:
    if not isinstance(o, dict):
        return None
    fn = o.get("function") if isinstance(o.get("function"), dict) else {}
    name = o.get("name") or fn.get("name")
    if name not in names:
        return None
    args = o.get("arguments", o.get("parameters", o.get("args", fn.get("arguments", fn.get("parameters")))))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return {"function": {"name": name, "arguments": json.dumps(args if isinstance(args, dict) else {})}}


def _text_tool_calls(text: str, names: set[str]) -> tuple[list[dict], str]:
    """Tool calls that small local models write into their reply text instead of the tool-call field:
    <tool_call>{...}</tool_call> (Qwen/Hermes), [TOOL_CALLS][...] (Mistral), <|python_tag|>{...} and
    <function=name>{...}</function> (Llama 3.x), ```json {...}``` or a reply that is just {"name": .., "arguments": ..}.
    Only used when the reply looks like a tool call, so JSON quoted in an explanation isn't run."""
    if not (_CALL_MARKERS.search(text) or text.strip()[:1] in ("{", "[")):
        return [], text
    calls, spans = [], []
    for m in re.finditer(r"<function=([\w.-]+)>\s*(\{.*?\})\s*</function>", text, re.S):
        try:
            c = _call_from({"name": m.group(1), "arguments": json.loads(m.group(2))}, names)
        except ValueError:
            c = None
        if c:
            calls.append(c)
            spans.append(m.span())
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        starts = [x for x in (text.find("{", i), text.find("[", i)) if x >= 0]
        if not starts:
            break
        s = min(starts)
        if any(a <= s < b for a, b in spans):
            i = s + 1
            continue
        try:
            obj, end = dec.raw_decode(text, s)
        except ValueError:
            i = s + 1
            continue
        found = [c for c in (_call_from(o, names) for o in (obj if isinstance(obj, list) else [obj])) if c]
        if found:
            calls += found
            spans.append((s, end))
        i = end
    if not calls:
        return [], text
    rest = text
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + rest[b:]
    rest = re.sub(r"</?tool_call>|<\|python_tag\|>|\[TOOL_CALLS\]|<\|eom_id\|>|<\|eot_id\|>|```(?:json|tool_call)?", "", rest)
    return calls, rest.strip()


def _clean_schema(s):
    """JSON schema for OpenAI-style function calling: drop pydantic titles and collapse Optional[...]
    (anyOf [X, null]) to X, which strict providers such as Gemini otherwise reject."""
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    if "anyOf" in s:
        opts = [o for o in s["anyOf"] if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(opts) == 1:
            merged = {**{k: v for k, v in s.items() if k != "anyOf"}, **opts[0]}
            return _clean_schema(merged)
    out = {}
    for k, v in s.items():
        if k == "title":
            continue
        if k == "default" and v is None:
            continue
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _clean_schema(sub) for name, sub in v.items()}  # property names may be "title"
        else:
            out[k] = v if k in ("enum", "required") else _clean_schema(v)
    if out.get("type") == "object" and "properties" not in out and "additionalProperties" not in out:
        out["properties"] = {}
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {}
    return out
