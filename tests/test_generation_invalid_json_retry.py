"""A malformed model reply is retried once with the same backend before falling back or failing."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm.generation_backend import GenerationError, GenerationErrorCode


def _analyzer():
    from src.analyzer import GeminiAnalyzer
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    analyzer.get_generation_backend_config_error = lambda: None
    analyzer._resolve_generation_backend_config = lambda: ("claude_code_cli", None)
    return analyzer


def _error(code=GenerationErrorCode.INVALID_JSON, stage="validation"):
    return GenerationError(error_code=code, stage=stage, retryable=False, fallbackable=True,
                           backend="claude_code_cli", provider="claude_code_cli", details={"reason": "invalid_json"})


def _ok():
    return SimpleNamespace(text='{"ok": true}', model="claude-test", usage={})


def test_invalid_reply_is_retried_once_and_the_retry_is_used():
    analyzer = _analyzer()
    backend = MagicMock()
    backend.generate.side_effect = [_error(), _ok()]
    with patch.object(analyzer, "_get_generation_backend", return_value=backend):
        text, model, _usage = analyzer._call_litellm("prompt", {})
    assert text == '{"ok": true}' and backend.generate.call_count == 2


def test_a_second_invalid_reply_fails_without_a_fallback():
    analyzer = _analyzer()
    backend = MagicMock()
    backend.generate.side_effect = [_error(), _error()]
    with patch.object(analyzer, "_get_generation_backend", return_value=backend), pytest.raises(GenerationError):
        analyzer._call_litellm("prompt", {})
    assert backend.generate.call_count == 2


def test_other_errors_are_not_retried():
    analyzer = _analyzer()
    backend = MagicMock()
    backend.generate.side_effect = [_error(GenerationErrorCode.COMMAND_NOT_FOUND, "configuration"), _ok()]
    with patch.object(analyzer, "_get_generation_backend", return_value=backend), pytest.raises(GenerationError):
        analyzer._call_litellm("prompt", {})
    assert backend.generate.call_count == 1


def test_after_a_failed_retry_the_fallback_still_runs():
    analyzer = _analyzer()
    analyzer._resolve_generation_backend_config = lambda: ("claude_code_cli", "litellm")
    primary, fallback = MagicMock(), MagicMock()
    primary.generate.side_effect = [_error(), _error()]
    fallback.generate.return_value = _ok()
    with patch.object(analyzer, "_get_generation_backend",
                      side_effect=lambda backend_id=None: primary if backend_id == "claude_code_cli" else fallback):
        text, _model, _usage = analyzer._call_litellm("prompt", {})
    assert text == '{"ok": true}' and primary.generate.call_count == 2 and fallback.generate.call_count == 1
