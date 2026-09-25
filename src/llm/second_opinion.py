# -*- coding: utf-8 -*-
"""Model tiers and independent second opinions.

Routine runs (scheduled reports, market review, automatic scans) use the
configured ``GENERATION_BACKEND``. Analyses a user explicitly requests run under
:func:`targeted_analysis`, which switches the primary backend to
``TARGETED_GENERATION_BACKEND`` and asks each ``SECOND_OPINION_BACKENDS``
backend for a short, independent verdict on the same data.

Second opinions are advisory: a failing or unavailable model is reported as
such and never blocks the primary analysis.
"""
from __future__ import annotations

import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

_TARGETED = contextvars.ContextVar("dsa_targeted_analysis", default=False)

OPINION_ACTIONS = ("buy", "hold", "sell", "watch")


@contextmanager
def targeted_analysis() -> Iterator[None]:
    """Mark analyses started in this context (and thread) as user-requested."""
    token = _TARGETED.set(True)
    try:
        yield
    finally:
        _TARGETED.reset(token)


def is_targeted() -> bool:
    return bool(_TARGETED.get())


def targeted_backend(config: Any) -> Optional[str]:
    """Primary backend for a user-requested analysis, or None to keep the routine one."""
    backend = str(getattr(config, "targeted_generation_backend", "") or "").strip().lower()
    return backend if backend and is_targeted() else None


def second_opinion_backends(config: Any, primary_backend: str) -> List[str]:
    if not is_targeted():
        return []
    return [item for item in (getattr(config, "second_opinion_backends", None) or []) if item != primary_backend]


def _codex_model() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        import tomllib

        with open(os.path.join(home, "config.toml"), "rb") as handle:
            return str(tomllib.load(handle).get("model") or "")
    except Exception:
        return ""


def model_label(backend_id: str, config: Any) -> str:
    """Human-readable model name for a backend, for reports and audit."""
    if backend_id == "litellm":
        model = str(getattr(config, "litellm_model", "") or "")
        # Channel routes use an ``openai/`` prefix; show the provider's own model id.
        return model.split("/", 1)[1] if model.startswith("openai/") else (model or "LiteLLM")
    if backend_id == "claude_code_cli":
        model = str(getattr(config, "claude_code_cli_model", "") or "")
        if is_targeted():
            model = str(getattr(config, "targeted_claude_code_cli_model", "") or "") or model
        return model or "Claude Code"
    if backend_id == "codex_cli":
        return _codex_model() or "Codex"
    if backend_id == "opencode_cli":
        return str(getattr(config, "opencode_cli_model", "") or "") or "OpenCode"
    return backend_id


def normalize_opinion(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate a model's verdict JSON; None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in OPINION_ACTIONS:
        return None
    try:
        score = int(round(float(raw.get("score"))))
    except (TypeError, ValueError):
        score = None
    if score is not None and not 0 <= score <= 100:
        score = None
    return {
        "action": action,
        "score": score,
        "reason": " ".join(str(raw.get("reason") or "").split())[:600],
        "risk": " ".join(str(raw.get("risk") or "").split())[:400],
    }


def collect_opinions(
    get_backend: Callable[[str], Any],
    backend_ids: Sequence[str],
    prompt: str,
    *,
    config: Any,
    system_prompt: Optional[str] = None,
    parse: Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]] = normalize_opinion,
    max_output_tokens: int = 2048,
) -> List[Dict[str, Any]]:
    """Ask each backend in parallel; every entry reports ``status`` ok/error."""
    from src.agent.runner import try_parse_json

    def ask(backend_id: str) -> Dict[str, Any]:
        # Second opinions belong to a user-requested analysis; worker threads do
        # not inherit the caller's context, so mark each call explicitly.
        with targeted_analysis():
            return _ask(backend_id)

    def _ask(backend_id: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"backend": backend_id, "model": model_label(backend_id, config)}
        try:
            result = get_backend(backend_id).generate(
                prompt,
                {"temperature": 0.2, "max_output_tokens": max_output_tokens},
                system_prompt=system_prompt,
            )
            opinion = parse(try_parse_json(result.text or ""))
            if opinion is None:
                return {**entry, "status": "error", "error": "The model did not return a valid verdict."}
            return {**entry, "status": "ok", **opinion}
        except Exception as exc:  # advisory only: never fail the primary analysis
            logger.warning("Second opinion from %s unavailable: %s", backend_id, type(exc).__name__)
            code = getattr(getattr(exc, "error_code", None), "value", None) or type(exc).__name__
            return {**entry, "status": "error", "error": f"Unavailable ({code})."}

    if not backend_ids:
        return []
    with ThreadPoolExecutor(max_workers=len(backend_ids), thread_name_prefix="second-opinion") as pool:
        return list(pool.map(ask, backend_ids))


def agreement(opinions: Sequence[Dict[str, Any]]) -> str:
    """'agree' when every available verdict has the same action, else 'split'."""
    actions = {item.get("action") for item in opinions if item.get("status") == "ok"}
    if len(actions) <= 1:
        return "agree" if actions else "unavailable"
    return "split"
