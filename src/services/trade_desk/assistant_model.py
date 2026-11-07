"""The models behind shadow mode and the Trade Desk questions: an ordered list, your coding plans first.

Two lists, each ``backend:model:effort`` entries tried in order until one gives readable JSON:

- ``TRADE_ASSISTANT_MODELS``: shadow mode (the trading assistant's notes, your questions, plan reading);
- ``TRADE_DESK_QUESTION_MODELS``: questions about your holdings and their daily review.

For example ``claude_code_cli:claude-sonnet-5-5:xhigh,codex_cli:gpt-6.1-sol:xhigh,
claude_code_cli:claude-opus-5-5:xhigh,litellm``. The backend is ``claude_code_cli`` (your Claude Code
plan), ``codex_cli`` (your Codex plan) or ``litellm`` (the API); the model and effort may be left out,
and then come from the backend's own setting. A chart image goes to every model with the prompt.

Without ``TRADE_ASSISTANT_MODELS`` the assistant keeps its first settings: ``TRADE_ASSISTANT_BACKENDS``
(default ``claude_code_cli,codex_cli,litellm``) with ``TRADE_ASSISTANT_CLAUDE_CODE_CLI_MODEL`` /
``_EFFORT`` (default ``CLAUDE_CODE_CLI_MODEL``, ``low``), ``TRADE_ASSISTANT_CODEX_CLI_MODEL`` / ``_EFFORT``
(default ``CODEX_CLI_MODEL``, ``low``) and ``TRADE_ASSISTANT_LITELLM_MODEL`` (default ``LITELLM_MODEL``).
Without ``TRADE_DESK_QUESTION_MODELS`` holding questions use the main generation backend, as before.

An entry that fails, is over its plan's usage limit, takes longer than the list's timeout
(``TRADE_ASSISTANT_TIMEOUT_SECONDS``, default 90; ``TRADE_DESK_QUESTION_TIMEOUT_SECONDS``, default 180)
or returns no readable JSON passes the request to the next. Each answer says which model gave it.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_BACKENDS = ("claude_code_cli", "codex_cli", "litellm")
BACKEND_NAMES = {"claude_code_cli": "Claude Code", "codex_cli": "Codex", "litellm": "API"}
EFFORTS = {"claude_code_cli": {"low", "medium", "high", "xhigh", "max"},
           "codex_cli": {"minimal", "low", "medium", "high", "xhigh"}}
CHAINS = {"assistant": ("TRADE_ASSISTANT_MODELS", "TRADE_ASSISTANT_TIMEOUT_SECONDS", 90),
          "questions": ("TRADE_DESK_QUESTION_MODELS", "TRADE_DESK_QUESTION_TIMEOUT_SECONDS", 180)}
_cache: Dict[Any, Any] = {}
_cache_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Entry:
    backend: str
    model: str = ""
    effort: str = ""
    legacy: bool = False  # from TRADE_ASSISTANT_BACKENDS: model and effort from the TRADE_ASSISTANT_* settings


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, "") or "").strip() or default


def parse_models(raw: str) -> List[Entry]:
    """``backend[:model[:effort]]`` entries, comma separated; ValueError names the first bad one."""
    entries = []
    for item in (part.strip() for part in (raw or "").split(",")):
        if not item:
            continue
        backend, model, effort = (item.split(":") + ["", ""])[:3]
        backend, model, effort = backend.strip(), model.strip(), effort.strip().lower()
        if backend not in BACKEND_NAMES or item.count(":") > 2:
            raise ValueError(f"'{item}': use backend:model:effort with a backend of {', '.join(BACKEND_NAMES)}")
        if effort and effort not in EFFORTS.get(backend, set()):
            allowed = ", ".join(sorted(EFFORTS.get(backend, set()))) or "none (the API takes no effort here)"
            raise ValueError(f"'{item}': effort for {backend} is one of {allowed}")
        entries.append(Entry(backend, model, effort))
    return entries


def configured(chain: str) -> bool:
    return bool(_env(CHAINS[chain][0]))


def entries(chain: str = "assistant") -> List[Entry]:
    variable = CHAINS[chain][0]
    raw = _env(variable)
    if raw:
        try:
            return parse_models(raw)
        except ValueError as exc:
            raise ValueError(f"{variable}: {exc}") from exc
    if chain != "assistant":
        raise ValueError(f"{variable} is not set")
    names = _env("TRADE_ASSISTANT_BACKENDS", ",".join(DEFAULT_BACKENDS))
    return [Entry(name.strip(), legacy=True) for name in names.split(",") if name.strip()]


def backends() -> List[str]:
    """The assistant's backends in order (for status pages)."""
    try:
        return [entry.backend for entry in entries("assistant")]
    except ValueError:
        return []


def _timeout(chain: str) -> int:
    _, variable, default = CHAINS[chain]
    try:
        return max(10, int(_env(variable, str(default))))
    except ValueError:
        return default


def _config(entry: Entry, timeout: int) -> Any:
    from src.config import get_config
    base = get_config()
    if entry.legacy:
        claude_model = _env("TRADE_ASSISTANT_CLAUDE_CODE_CLI_MODEL", base.claude_code_cli_model)
        claude_effort = _env("TRADE_ASSISTANT_CLAUDE_CODE_CLI_EFFORT", "low").lower()
        codex_model = _env("TRADE_ASSISTANT_CODEX_CLI_MODEL", base.codex_cli_model)
        codex_effort = _env("TRADE_ASSISTANT_CODEX_CLI_EFFORT", "low").lower()
        litellm_model = _env("TRADE_ASSISTANT_LITELLM_MODEL", base.litellm_model)
    else:
        claude_model = entry.model or base.claude_code_cli_model
        claude_effort = entry.effort or base.claude_code_cli_effort
        codex_model = entry.model or base.codex_cli_model
        codex_effort = entry.effort or base.codex_cli_effort
        litellm_model = entry.model or _env("TRADE_ASSISTANT_LITELLM_MODEL", base.litellm_model)
    # The targeted_* copies keep a user-requested analysis's TARGETED_* models from replacing this entry's.
    return dataclasses.replace(
        base,
        claude_code_cli_model=claude_model, claude_code_cli_effort=claude_effort,
        targeted_claude_code_cli_model=claude_model, targeted_claude_code_cli_effort=claude_effort,
        codex_cli_model=codex_model, codex_cli_effort=codex_effort,
        targeted_codex_cli_model=codex_model, targeted_codex_cli_effort=codex_effort,
        litellm_model=litellm_model, litellm_fallback_models=[],
        generation_backend_timeout_seconds=timeout,
    )


def _analyzer(entry: Entry, timeout: int) -> Any:
    """One analyzer per entry, built once (it sets up LiteLLM's router)."""
    key = (entry, timeout)
    with _cache_lock:
        if key not in _cache:
            from src.analyzer import GeminiAnalyzer
            _cache[key] = GeminiAnalyzer(config=_config(entry, timeout))
        return _cache[key]


def _settings(entry: Entry) -> Tuple[str, str]:
    try:
        config = _config(entry, 90)
    except Exception:
        return entry.model, entry.effort
    return {"claude_code_cli": (config.claude_code_cli_model, config.claude_code_cli_effort),
            "codex_cli": (config.codex_cli_model, config.codex_cli_effort),
            "litellm": (config.litellm_model, "")}.get(entry.backend, (entry.model, entry.effort))


def label(entry: Entry, model: str = "") -> str:
    """``Claude Code (claude-sonnet-5-5, xhigh)``: the backend, and the model and effort it was set to use."""
    configured_model, effort = _settings(entry)
    model = configured_model if not model or model == entry.backend else model  # local CLIs report their own id
    detail = ", ".join(part for part in (model, effort) if part)
    name = BACKEND_NAMES.get(entry.backend, entry.backend)
    return f"{name} ({detail})" if detail else name


def describe(chain: str = "assistant") -> str:
    """The order in words, for the page: ``Claude Code (claude-sonnet-5-5, xhigh), then Codex (...)``."""
    key = ("describe", chain, _env(CHAINS[chain][0]), _env("TRADE_ASSISTANT_BACKENDS"))
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        text = ", then ".join(label(entry) for entry in entries(chain))
    except ValueError as exc:
        text = str(exc)
    with _cache_lock:
        _cache[key] = text
    return text


def generate(prompt: str, system_prompt: str, *, chain: str = "assistant", images: Optional[List[Dict[str, str]]] = None,
             validate: Optional[Callable[[Dict[str, Any]], None]] = None, max_output_tokens: int = 2000,
             analyzer_for: Optional[Callable[[Entry], Any]] = None) -> Dict[str, Any]:
    """The first entry's readable JSON answer: ``data``, ``backend``, ``model``, ``label``, ``seconds``, ``usage``, ``tried``.

    ``images`` (``{"mime_type", "data"}``, base64) go with the prompt to every entry.
    """
    from src.agent.runner import try_parse_json
    timeout = _timeout(chain)
    tried: List[Dict[str, Any]] = []
    config = {"temperature": 0.2, "max_output_tokens": max_output_tokens}
    if images:
        config["images"] = images
    try:
        chain_entries = entries(chain)
    except ValueError as exc:
        raise RuntimeError(f"no model answered: {exc}") from exc
    for entry in chain_entries:
        started = time.monotonic()
        try:
            analyzer = analyzer_for(entry) if analyzer_for is not None else _analyzer(entry, timeout)
            backend = analyzer._get_generation_backend(entry.backend)
            result = backend.generate(prompt, config, system_prompt=system_prompt)
            data = try_parse_json((getattr(result, "text", "") or "").strip())
            if not isinstance(data, dict):
                raise ValueError("no readable JSON")
            if validate is not None:
                validate(data)
            seconds = round(time.monotonic() - started, 2)
            model = str(getattr(result, "model", "") or "")
            return {"data": data, "backend": entry.backend, "model": model, "label": label(entry, model),
                    "seconds": seconds, "usage": dict(getattr(result, "usage", None) or {}), "tried": tried}
        except Exception as exc:  # the next entry answers
            reason = str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
            tried.append({"backend": entry.backend, "model": entry.model, "reason": reason,
                          "seconds": round(time.monotonic() - started, 2)})
            logger.info("Trade Desk model %s (%s) failed: %s", entry.backend, entry.model or "default", reason)
    raise RuntimeError("no model answered: " + "; ".join(f"{t['backend']} {t['reason']}" for t in tried))
