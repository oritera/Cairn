"""Extract a structured network-topology graph from a project's free-text facts.

Facts in Cairn are prose written by agents, so there is no structured host/cred
data to draw from. This module asks an available LLM to read all facts and emit a
strict JSON topology: hosts (ports, creds found, creds that work, access level,
zone) and edges (visibility / pivot / cred-works). The result is cached by caller.

Models come from three places, merged into one list by :func:`list_models`:

* The **running dispatcher's** workers — discovered from the `--config` of the
  live `cairn dispatch` process. Any worker that carries an API endpoint + key
  (pi / GLM / Qwen / DeepSeek via OpenAI-compatible or Anthropic HTTP, or a
  container-mode claudecode) becomes a usable extractor. This is how "whatever
  neural net you plugged in" shows up automatically.
* Host **CLIs** on PATH — `claude` (sonnet/opus/haiku) and `codex` (ChatGPT).
* A fully custom command via the CAIRN_TOPO_CMD env var (reads prompt on stdin,
  prints JSON on stdout) — any other backend.

The recommended (green) option is the running dispatcher's primary worker when
there is one, else the best available host CLI. API keys are read server-side
only and never included in what :func:`list_models` returns to the browser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT_S = int(os.environ.get("CAIRN_TOPO_TIMEOUT", "300"))
# Generous output budget: "thinking" models (e.g. GLM) spend part of it on
# reasoning tokens, so a small cap leaves nothing for the JSON itself.
_MAX_TOKENS = int(os.environ.get("CAIRN_TOPO_MAX_TOKENS", "16384"))

# Host-CLI extractor options. `cli` must be on PATH for the option to be usable.
_MODELS: list[dict] = [
    {
        "id": "claude-sonnet",
        "label": "Claude Sonnet",
        "provider": "claude",
        "cli": "claude",
        "model": "sonnet",
        "note": "Fast and reliable on security notes.",
    },
    {
        "id": "claude-opus",
        "label": "Claude Opus",
        "provider": "claude",
        "cli": "claude",
        "model": "opus",
        "note": "Most capable, but ~2.5x slower.",
    },
    {
        "id": "claude-haiku",
        "label": "Claude Haiku",
        "provider": "claude",
        "cli": "claude",
        "model": "haiku",
        "note": "Not recommended: cyber safeguards reject offensive-security notes.",
        "discouraged": True,
    },
    {
        "id": "codex",
        "label": "ChatGPT (Codex)",
        "provider": "codex",
        "cli": "codex",
        "model": None,  # use whatever model the codex CLI is logged into
        "note": "Uses your ChatGPT / codex login.",
    },
]

# First available host CLI wins when there is no dispatcher model to recommend.
_PREFERENCE = ["claude-sonnet", "codex", "claude-opus", "claude-haiku"]

_SCHEMA_HINT = """\
Output ONLY one raw JSON object (no markdown, no prose, no code fences) with this shape:

{
  "hosts": [
    {
      "id": "<ip or hostname, stable key>",
      "name": "<short label, e.g. 'MSSQL' or 'dc.corp.pipeline.stf'>",
      "zone": "attacker|external|dmz|internal|ics",
      "ports": [{"port": 1433, "service": "MSSQL"}],
      "creds_found": ["user:pass or key description discovered ON/ABOUT this host"],
      "creds_work": ["cred that is CONFIRMED working against this host, with the level it grants"],
      "access": "none|user|admin|system|root",
      "notes": "<one short line: how it was compromised / why it matters>"
    }
  ],
  "edges": [
    {"from": "<host id>", "to": "<host id>", "type": "visibility|pivot|cred-works", "label": "<optional short>"}
  ]
}

Rules:
- Always include a host with id "attacker" (zone "attacker") for our attacking machine.
- Include every host, subnet pivot, and ICS/OT target mentioned in the facts.
- "access" is the HIGHEST confirmed foothold on that host: root (Linux root), system (NT SYSTEM/SYSTEM),
  admin (local/domain admin or www-data+SUID-to-root path not yet run -> use user), user (any shell/valid login), none (only scanned).
- edge "visibility" = X can see/scan Y; "pivot" = we route through X to reach Y; "cred-works" = a credential proven to authenticate X->Y.
- Deduplicate hosts by IP. Keep notes to one short sentence. Do not invent data not supported by the facts.
"""


# --------------------------------------------------------------------------- #
# Dispatcher discovery
# --------------------------------------------------------------------------- #
def _active_config_path() -> Path | None:
    """Find the --config of the running `cairn dispatch` process, if any."""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [a for a in raw.decode("utf-8", "replace").split("\x00") if a]
        if not args or "--config" not in args:
            continue
        joined = " ".join(args)
        if "dispatch" not in joined or "cairn" not in joined:
            continue
        cfg = args[args.index("--config") + 1] if args.index("--config") + 1 < len(args) else None
        if not cfg:
            continue
        path = Path(cfg)
        if not path.is_absolute():
            try:
                cwd = os.readlink(entry / "cwd")
                path = Path(cwd) / cfg
            except OSError:
                pass
        if path.exists():
            return path
    return None


def _worker_backend(env: dict) -> dict | None:
    """If a worker's env carries an API endpoint + key, return its connection
    dict (kept server-side only). Local-mode workers return None — they are the
    host CLIs already listed separately."""
    if env.get("PI_BASE_URL") and env.get("PI_API_KEY") and env.get("PI_MODEL"):
        return {"api": env.get("PI_PROVIDER_API") or "openai-completions",
                "base_url": env["PI_BASE_URL"], "key": env["PI_API_KEY"], "model": env["PI_MODEL"]}
    if env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_AUTH_TOKEN") and env.get("ANTHROPIC_MODEL"):
        return {"api": "anthropic", "base_url": env["ANTHROPIC_BASE_URL"],
                "key": env["ANTHROPIC_AUTH_TOKEN"], "model": env["ANTHROPIC_MODEL"]}
    if env.get("OPENAI_BASE_URL") and env.get("OPENAI_API_KEY") and env.get("OPENAI_MODEL"):
        return {"api": "openai-completions", "base_url": env["OPENAI_BASE_URL"],
                "key": env["OPENAI_API_KEY"], "model": env["OPENAI_MODEL"]}
    return None


def _discover_workers() -> list[dict]:
    """API-reachable models from the live dispatcher config, highest priority first."""
    path = _active_config_path()
    if path is None:
        fallback = Path("dispatch.yaml")
        path = fallback if fallback.exists() else None
    if path is None:
        return []
    try:
        from cairn.dispatcher.config import DispatchConfig
        cfg = DispatchConfig.load(path)
    except Exception:
        return []
    out: list[dict] = []
    for w in sorted(cfg.workers, key=lambda x: x.priority):
        backend = _worker_backend(dict(w.env))
        if backend is None:
            continue
        host = urllib.parse.urlparse(backend["base_url"]).netloc or backend["base_url"]
        out.append({
            "id": f"dispatch:{w.name}",
            "label": backend["model"],
            "backend": backend,  # server-side only; stripped before returning to UI
            "note": f"Running dispatcher worker '{w.name}' — {backend['model']} via {host}",
            "priority": w.priority,
        })
    return out


# --------------------------------------------------------------------------- #
# Model listing / selection
# --------------------------------------------------------------------------- #
def _custom_cmd() -> list[str] | None:
    raw = os.environ.get("CAIRN_TOPO_CMD")
    return raw.split() if raw else None


def _entry(model_id: str) -> dict | None:
    return next((m for m in _MODELS if m["id"] == model_id), None)


def _recommended(discovered: list[dict], available: dict[str, bool], custom: bool) -> str | None:
    """Green pick: the dispatcher's primary (highest-priority) worker if any,
    else a custom command, else the best available host CLI."""
    if discovered:
        return discovered[0]["id"]
    if custom:
        return "custom"
    for mid in _PREFERENCE:
        if available.get(mid):
            return mid
    return None


def list_models() -> dict:
    """Describe extractor models for the settings UI (availability + recommended).

    Never includes API keys. Returns {"models": [...], "default": <id|None>}.
    """
    discovered = _discover_workers()
    available = {m["id"]: bool(shutil.which(m["cli"])) for m in _MODELS}
    custom = _custom_cmd() is not None
    recommended = _recommended(discovered, available, custom)

    models: list[dict] = []
    for d in discovered:  # dispatcher-configured models first
        models.append({
            "id": d["id"],
            "label": d["label"],
            "provider": "dispatcher",
            "available": True,
            "recommended": d["id"] == recommended,
            "discouraged": False,
            "note": d["note"],
            "unavailable_reason": None,
        })
    if custom:
        models.append({
            "id": "custom",
            "label": "Custom (CAIRN_TOPO_CMD)",
            "provider": "custom",
            "available": True,
            "recommended": recommended == "custom",
            "discouraged": False,
            "note": "Uses the command in CAIRN_TOPO_CMD (any backend).",
            "unavailable_reason": None,
        })
    for m in _MODELS:
        ok = available[m["id"]]
        models.append({
            "id": m["id"],
            "label": m["label"],
            "provider": m["provider"],
            "available": ok,
            "recommended": m["id"] == recommended,
            "discouraged": m.get("discouraged", False),
            "note": m["note"],
            "unavailable_reason": None if ok else f"{m['cli']} CLI not found on PATH",
        })
    return {"models": models, "default": recommended}


def _default_model_id() -> str | None:
    """Pick the model when the caller did not specify one."""
    env = os.environ.get("CAIRN_TOPO_MODEL")
    if env:
        return env
    return list_models()["default"]


# --------------------------------------------------------------------------- #
# Prompt / parsing
# --------------------------------------------------------------------------- #
def _build_prompt(origin: str, goal: str, facts: list[dict]) -> str:
    lines = [
        "You are a penetration-test note analyst. Read the ORIGIN, GOAL and FACTS of an",
        "engagement and build a network topology graph from them.",
        "",
        f"ORIGIN:\n{origin}",
        "",
        f"GOAL:\n{goal}",
        "",
        "FACTS (chronological, free text written by the testing agents):",
    ]
    for f in facts:
        fid = f.get("id", "?")
        desc = (f.get("description") or "").strip()
        lines.append(f"\n[{fid}]\n{desc}")
    lines.append("\n\n" + _SCHEMA_HINT)
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict) or "hosts" not in obj:
        raise ValueError("model output is not a topology object")
    obj.setdefault("edges", [])
    return obj


def _guard_refusals(text: str) -> None:
    low = text.lower()
    if "session limit" in low or "usage limit" in low or "rate limit" in low:
        raise RuntimeError(f"model backend is rate/usage limited: {text.strip()[:200]}")
    if "[cyber]" in low or "safeguards flagged" in low:
        raise RuntimeError(
            "model refused the content via cyber safeguards — pick a different "
            "model (Claude Sonnet/Opus handle security notes)"
        )


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
def _run_claude(exe: str, model: str | None, prompt: str) -> str:
    # Pure text->JSON transform: disable all tools so the model answers directly.
    cmd = [exe, "-p", "--tools", "", "--max-turns", "1"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S)
    _guard_refusals(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"extractor exited {proc.returncode}: {tail}")
    return proc.stdout


def _run_codex(exe: str, model: str | None, prompt: str) -> str:
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        outfile = tf.name
    try:
        cmd = [exe, "exec", "-s", "read-only", "--skip-git-repo-check", "--color", "never", "-o", outfile]
        if model:
            cmd += ["-m", model]
        cmd.append("-")
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S)
        _guard_refusals(proc.stdout + "\n" + proc.stderr)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(f"extractor exited {proc.returncode}: {tail}")
        try:
            out = Path(outfile).read_text(encoding="utf-8")
        except OSError:
            out = proc.stdout
        return out or proc.stdout
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass


def _http_text(payload: dict, api: str) -> str:
    """Pull the assistant text out of an OpenAI/Anthropic-style response."""
    if "anthropic" in api:
        parts = payload.get("content")
        if isinstance(parts, list):
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    if "responses" in api:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]
        chunks = []
        for item in payload.get("output", []) or []:
            for c in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    chunks.append(c["text"])
        if chunks:
            return "\n".join(chunks)
    # openai chat/completions
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    return json.dumps(payload)[:2000]


def _post_json(url: str, headers: dict, body: dict) -> dict:
    """POST JSON and return the parsed response. If the server rejects an
    optional tuning field (e.g. ``thinking`` on a non-GLM endpoint) with a 4xx,
    retry once without it so the call still works on strict backends."""
    host = urllib.parse.urlparse(url).netloc
    attempts = [body]
    if "thinking" in body:
        stripped = {k: v for k, v in body.items() if k != "thinking"}
        attempts.append(stripped)
    last_err = None
    for attempt in attempts:
        req = urllib.request.Request(
            url, data=json.dumps(attempt).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
            last_err = RuntimeError(f"HTTP {e.code} from {host}: {detail}")
            if e.code in (400, 422) and attempt is not attempts[-1]:
                continue  # likely the optional field; retry without it
            raise last_err
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach {host}: {e.reason}")
        except (TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(f"bad response from {host}: {e}")
    raise last_err or RuntimeError(f"no response from {host}")


def _run_http(backend: dict, prompt: str) -> str:
    """Call an OpenAI-compatible or Anthropic HTTP endpoint (GLM/Qwen/etc.)."""
    api = backend["api"]
    base = backend["base_url"].rstrip("/")
    key = backend["key"]
    model = backend["model"]
    if "anthropic" in api:
        url = f"{base}/v1/messages"
        headers = {"content-type": "application/json", "x-api-key": key,
                   "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": _MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}]}
    elif "responses" in api:
        url = f"{base}/responses"
        headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        body = {"model": model, "input": [{"role": "user", "content": prompt}],
                "max_output_tokens": _MAX_TOKENS, "stream": False}
    else:  # openai-completions and anything else
        url = f"{base}/chat/completions"
        headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": _MAX_TOKENS, "stream": False}
        # Turn off "thinking" for reasoning models (e.g. GLM): otherwise they can
        # burn the whole output budget on reasoning and return empty content.
        # GLM-compatible knob; stripped and retried if the server rejects it.
        body["thinking"] = {"type": "disabled"}

    payload = _post_json(url, headers, body)
    text = _http_text(payload, api)
    if not text.strip():
        finish = None
        try:
            finish = (payload.get("choices") or [{}])[0].get("finish_reason")
        except Exception:
            finish = None
        usage = payload.get("usage") or {}
        raise RuntimeError(
            f"model returned empty content (finish_reason={finish}, usage={usage}); "
            f"a 'thinking' model likely spent the whole output budget on reasoning — "
            f"raise CAIRN_TOPO_MAX_TOKENS (currently {_MAX_TOKENS}) or pick another model"
        )
    _guard_refusals(text)
    return text


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def extract_topology(origin: str, goal: str, facts: list[dict], model_id: str | None = None) -> dict:
    """Run an available model over the facts and return the parsed topology dict.

    ``model_id`` selects an option from :func:`list_models`; None auto-picks.
    Raises RuntimeError with a readable message on any failure.
    """
    prompt = _build_prompt(origin, goal, facts)
    model_id = model_id or _default_model_id()
    if not model_id:
        raise RuntimeError("no extractor model available (no dispatcher worker, CLI, or CAIRN_TOPO_CMD)")

    try:
        if model_id.startswith("dispatch:"):
            backend = next((d["backend"] for d in _discover_workers() if d["id"] == model_id), None)
            if backend is None:
                raise RuntimeError(f"model '{model_id}' is not in the active dispatcher config anymore")
            raw = _run_http(backend, prompt)
        elif model_id == "custom":
            custom = _custom_cmd()
            if not custom:
                raise RuntimeError("custom model selected but CAIRN_TOPO_CMD is not set")
            proc = subprocess.run(custom, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S)
            _guard_refusals(proc.stdout + "\n" + proc.stderr)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                raise RuntimeError(f"extractor exited {proc.returncode}: {tail}")
            raw = proc.stdout
        else:
            entry = _entry(model_id)
            if entry is None:
                raise RuntimeError(f"unknown extractor model '{model_id}'")
            exe = shutil.which(entry["cli"])
            if not exe:
                raise RuntimeError(f"{entry['cli']} CLI not found on PATH for model '{model_id}'")
            if entry["provider"] == "codex":
                raw = _run_codex(exe, entry["model"], prompt)
            else:
                raw = _run_claude(exe, entry["model"], prompt)
    except FileNotFoundError:
        raise RuntimeError(f"extractor command not found (model '{model_id}')")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"topology extraction timed out after {TIMEOUT_S}s")

    try:
        return _parse_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not parse topology JSON: {e}")
