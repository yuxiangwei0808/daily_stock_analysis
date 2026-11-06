"""The trading assistant's model: your Claude Code plan first, then Codex, and the API only as a fallback.

``TRADE_ASSISTANT_BACKENDS`` (default ``claude_code_cli,codex_cli,litellm``) is tried in order, each with
the assistant's own light settings, so it never spends the TARGETED_* models of user-requested analyses:

- Claude Code: ``TRADE_ASSISTANT_CLAUDE_CODE_CLI_MODEL`` (default ``CLAUDE_CODE_CLI_MODEL``) at
  ``TRADE_ASSISTANT_CLAUDE_CODE_CLI_EFFORT`` (default ``low``);
- Codex: ``TRADE_ASSISTANT_CODEX_CLI_MODEL`` (default ``CODEX_CLI_MODEL``) at
  ``TRADE_ASSISTANT_CODEX_CLI_EFFORT`` (default ``low``);
- LiteLLM: ``TRADE_ASSISTANT_LITELLM_MODEL`` (default ``LITELLM_MODEL``; it must be one of a configured
  channel's models), with no further fallback models.

A backend that fails, is over its usage limit, takes longer than ``TRADE_ASSISTANT_TIMEOUT_SECONDS``
(default 90) or returns no readable JSON passes the request to the next. Each answer says which
backend and model gave it and how long it took.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BACKENDS = ("claude_code_cli", "codex_cli", "litellm")
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, "") or "").strip() or default


def backends() -> List[str]:
    raw = _env("TRADE_ASSISTANT_BACKENDS", ",".join(DEFAULT_BACKENDS))
    return [name.strip() for name in raw.split(",") if name.strip()]


def _config() -> Any:
    from src.config import get_config
    base = get_config()
    timeout = int(_env("TRADE_ASSISTANT_TIMEOUT_SECONDS", "90"))
    return dataclasses.replace(
        base,
        claude_code_cli_model=_env("TRADE_ASSISTANT_CLAUDE_CODE_CLI_MODEL", base.claude_code_cli_model),
        claude_code_cli_effort=_env("TRADE_ASSISTANT_CLAUDE_CODE_CLI_EFFORT", "low").lower(),
        codex_cli_model=_env("TRADE_ASSISTANT_CODEX_CLI_MODEL", base.codex_cli_model),
        codex_cli_effort=_env("TRADE_ASSISTANT_CODEX_CLI_EFFORT", "low").lower(),
        litellm_model=_env("TRADE_ASSISTANT_LITELLM_MODEL", base.litellm_model),
        litellm_fallback_models=[],
        generation_backend_timeout_seconds=timeout,
    )


def _analyzer() -> Any:
    """One analyzer with the assistant's settings, built once (it sets up LiteLLM's router)."""
    with _cache_lock:
        if "analyzer" not in _cache:
            from src.analyzer import GeminiAnalyzer
            _cache["analyzer"] = GeminiAnalyzer(config=_config())
        return _cache["analyzer"]


def label(backend_id: str, model: str = "") -> str:
    """``Claude Code (claude-sonnet-5-5)``: the backend and the model it was set to use."""
    names = {"claude_code_cli": "Claude Code", "codex_cli": "Codex", "litellm": "API"}
    if not model or model == backend_id:  # local CLIs report their own id
        try:
            config = _config()
            model = {"claude_code_cli": config.claude_code_cli_model, "codex_cli": config.codex_cli_model,
                     "litellm": config.litellm_model}.get(backend_id, "")
        except Exception:
            model = ""
    name = names.get(backend_id, backend_id)
    return f"{name} ({model})" if model else name


def generate(prompt: str, system_prompt: str, *, validate: Optional[Callable[[Dict[str, Any]], None]] = None,
             analyzer: Any = None) -> Dict[str, Any]:
    """The first backend's readable JSON answer: ``data``, ``backend``, ``model``, ``seconds``, ``usage``, ``tried``."""
    from src.agent.runner import try_parse_json
    analyzer = analyzer or _analyzer()
    tried: List[Dict[str, Any]] = []
    for backend_id in backends():
        started = time.monotonic()
        try:
            backend = analyzer._get_generation_backend(backend_id)
            result = backend.generate(prompt, {"temperature": 0.2, "max_output_tokens": 2000}, system_prompt=system_prompt)
            data = try_parse_json((getattr(result, "text", "") or "").strip())
            if not isinstance(data, dict):
                raise ValueError("no readable JSON")
            if validate is not None:
                validate(data)
            seconds = round(time.monotonic() - started, 2)
            model = str(getattr(result, "model", "") or "")
            return {"data": data, "backend": backend_id, "model": model, "label": label(backend_id, model),
                    "seconds": seconds, "usage": dict(getattr(result, "usage", None) or {}), "tried": tried}
        except Exception as exc:  # the next backend answers
            reason = str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
            tried.append({"backend": backend_id, "reason": reason, "seconds": round(time.monotonic() - started, 2)})
            logger.info("Trading assistant model %s failed: %s", backend_id, reason)
    raise RuntimeError("no model answered: " + "; ".join(f"{t['backend']} {t['reason']}" for t in tried))
