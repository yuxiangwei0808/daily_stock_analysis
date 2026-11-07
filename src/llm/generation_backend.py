# -*- coding: utf-8 -*-
"""Shared generation backend contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol


class GenerationErrorCode(str, Enum):
    """Structured generation backend error codes.

    Shared across LiteLLM and local CLI generation backends.
    """

    BACKEND_NOT_CONFIGURED = "backend_not_configured"
    COMMAND_NOT_FOUND = "command_not_found"
    COMMAND_NOT_EXECUTABLE = "command_not_executable"
    TIMEOUT = "timeout"
    NON_ZERO_EXIT = "non_zero_exit"
    EMPTY_OUTPUT = "empty_output"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNSUPPORTED_TOOL_CALLING = "unsupported_tool_calling"
    INTERACTIVE_PROMPT_REQUIRED = "interactive_prompt_required"
    APPROVAL_REQUIRED = "approval_required"
    LOGIN_REQUIRED = "login_required"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    UNSAFE_CONFIG = "unsafe_config"
    UNKNOWN_BACKEND_ERROR = "unknown_backend_error"


@dataclass(frozen=True)
class GenerationCapabilities:
    """Backend capability flags surfaced to resolvers and diagnostics."""

    supports_json: bool
    supports_tools: bool
    supports_stream: bool
    supports_vision: bool
    supports_health_check: bool
    supports_smoke_test: bool


@dataclass
class GenerationResult:
    """Normalized result returned by generation backends."""

    text: str
    model: str
    provider: str
    backend: str
    usage: Dict[str, Any]
    raw: Any = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationError(Exception):
    """Structured generation backend failure.

    ``stage`` is intentionally descriptive rather than a closed enum. Current
    generation paths use values such as ``generation``, ``configuration``,
    ``execution``, ``validation``, and ``fallback``.
    """

    error_code: GenerationErrorCode
    stage: str
    retryable: bool
    fallbackable: bool
    backend: str
    provider: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider:
            self.provider = self.backend
        Exception.__init__(self, self.message)

    @property
    def message(self) -> str:
        return f"{self.error_code.value} at {self.stage} for backend {self.backend}"


class GenerationBackend(Protocol):
    """Protocol implemented by generation backends."""

    backend_id: str
    capabilities: GenerationCapabilities

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        """Generate text with the backend."""


IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_GENERATION_IMAGES = 8


def generation_images(generation_config: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Images attached to a request: ``generation_config["images"]``, each ``{"mime_type", "data"}`` (base64).

    Backends that can read images send them with the prompt (Claude Code and Codex CLIs, LiteLLM);
    a request without the key is text only, as before. Malformed entries raise ValueError.
    """
    raw = (generation_config or {}).get("images") or []
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_GENERATION_IMAGES:
        raise ValueError(f"images must be a list of at most {MAX_GENERATION_IMAGES}")
    out: List[Dict[str, str]] = []
    for item in raw:
        mime = str((item or {}).get("mime_type") or "")
        data = str((item or {}).get("data") or "")
        if mime not in IMAGE_MIME_TYPES or not data:
            raise ValueError("each image needs a supported mime_type and base64 data")
        out.append({"mime_type": mime, "data": data})
    return out
