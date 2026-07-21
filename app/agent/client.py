"""Async Qwen-only chat client with ordered provider/model failover [FR-12]."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.config import (
    DEFAULT_OPENROUTER_FAST_MODEL,
    OPENROUTER_BASE_URL,
    RUNTIME_OPENROUTER_MODELS,
    RUNTIME_QWENCLOUD_MODELS,
    Settings,
    validate_qwen_base_url,
)
from app.trail import DecisionTrailStore, TrailEntryType

DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 15.0
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0
_FALLBACK_STATUS_CODES = frozenset({401, 402, 403, 404, 408, 429})
_MAX_FALLBACK_ERROR_BYTES = 4_096
_MAX_FALLBACK_ERROR_TEXT_CHARS = 1_024
_MODEL_UNAVAILABLE_CODES = frozenset(
    {
        "invalid_model",
        "invalidmodel",
        "model_does_not_exist",
        "modeldoesnotexist",
        "model_not_available",
        "modelnotavailable",
        "model_not_found",
        "modelnotfound",
        "model_unavailable",
        "modelunavailable",
        "unknown_model",
        "unknownmodel",
    }
)
_MODEL_UNAVAILABLE_MESSAGES = (
    re.compile(
        r"\bmodel(?:\s+(?:id|name))?\b.{0,96}"
        r"\b(?:does\s+not\s+exist|not\s+available|not\s+found|unavailable)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:invalid|unknown)\s+model(?:\s+(?:id|name))?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnot\s+a\s+valid\s+model(?:\s+id)?\b", re.IGNORECASE),
    re.compile(r"\bno\s+endpoints?\s+found\s+for\b", re.IGNORECASE),
)
_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_MISSING = object()
_LOGGER = logging.getLogger("praxis.agent.client")


def _contains_non_scalar_unicode(value: Any) -> bool:
    """Return whether a provider response contains any lone surrogate."""

    pending = [value]
    seen_containers: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                return True
            continue
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(current)
    return False


class ModelRole(str, Enum):
    """The two model roles fixed by ADR-005 and ADR-009."""

    PRIMARY = "primary"
    FAST = "fast"


class QwenClientError(RuntimeError):
    """Base class for deliberately redacted runtime-client failures."""


class QwenConfigurationError(QwenClientError):
    """Raised when the configured Qwen-only route cannot be used safely."""


class QwenCallError(QwenClientError):
    """A terminal provider response that is not eligible for fallback."""

    def __init__(self, provider: str, model: str, reason: str) -> None:
        self.provider = provider
        self.model = model
        self.reason = reason
        super().__init__(
            f"Qwen call failed for {provider}/{model} (reason={reason})"
        )


class QwenExhaustedError(QwenClientError):
    """All configured Qwen attempts ended in documented fallback failures."""

    def __init__(self, provider: str, model: str, reason: str) -> None:
        self.provider = provider
        self.model = model
        self.reason = reason
        super().__init__(
            f"Qwen route exhausted at {provider}/{model} (reason={reason})"
        )


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """A provider-tagged, non-streaming chat completion response."""

    provider: str
    model: str
    _response: dict[str, Any] = field(repr=False)

    @classmethod
    def from_response(
        cls,
        provider: str,
        model: str,
        response: Any,
    ) -> "ChatCompletion":
        if not isinstance(response, dict):
            raise QwenCallError(provider, model, "invalid_response")
        if _contains_non_scalar_unicode(response):
            raise QwenCallError(provider, model, "invalid_response")

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise QwenCallError(provider, model, "invalid_response")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise QwenCallError(provider, model, "invalid_response")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise QwenCallError(provider, model, "invalid_response")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and (
            not isinstance(tool_calls, list)
            or any(not isinstance(item, dict) for item in tool_calls)
        ):
            raise QwenCallError(provider, model, "invalid_response")

        isolated_response: dict[str, Any] | None
        try:
            isolated_response = copy.deepcopy(response)
        except RecursionError:
            isolated_response = None
        if isolated_response is None:
            # Raise outside the active parser/copy exception so the public
            # error cannot retain a provider-controlled object graph through
            # ``__context__``.
            raise QwenCallError(provider, model, "invalid_response")

        return cls(
            provider=provider,
            model=model,
            _response=isolated_response,
        )

    @property
    def raw_response(self) -> dict[str, Any]:
        """Return an isolated copy for later triage and tool-loop processing."""

        return copy.deepcopy(self._response)

    @property
    def choice(self) -> dict[str, Any]:
        return copy.deepcopy(self._response["choices"][0])

    @property
    def message(self) -> dict[str, Any]:
        return copy.deepcopy(self._response["choices"][0]["message"])

    @property
    def content(self) -> Any:
        """Return provider-normalized message content without coercing JSON."""

        return copy.deepcopy(self._response["choices"][0]["message"].get("content"))

    @property
    def visible_content(self) -> Any:
        """Remove an inline thinking block while preserving non-string content."""

        content = self.content
        if not isinstance(content, str):
            return content
        return _THINK_BLOCK.sub("", content).strip()

    @property
    def reasoning_content(self) -> str | None:
        """Read Qwen thinking output across both compatible-mode variants."""

        message = self._response["choices"][0]["message"]
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        content = message.get("content")
        if isinstance(content, str):
            match = _THINK_BLOCK.search(content)
            if match:
                return match.group(1).strip()
        return None

    @property
    def tool_calls(self) -> tuple[dict[str, Any], ...]:
        calls = self._response["choices"][0]["message"].get("tool_calls") or []
        return tuple(copy.deepcopy(item) for item in calls)

    @property
    def finish_reason(self) -> str | None:
        value = self._response["choices"][0].get("finish_reason")
        return value if isinstance(value, str) else None

    @property
    def usage(self) -> dict[str, Any]:
        value = self._response.get("usage")
        return copy.deepcopy(value) if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class _Attempt:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)

    @property
    def route(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(slots=True)
class _RouteProgress:
    """Track only the active safe route label for logical-deadline errors."""

    current: _Attempt | None = None


class QwenClient:
    """Call Qwen Cloud first and enter OpenRouter only on accepted failures."""

    def __init__(
        self,
        settings: Settings,
        *,
        trail: DecisionTrailStore | None = None,
        attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._validate_timeout(
            "attempt_timeout_seconds",
            attempt_timeout_seconds,
            maximum=DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
        )
        self._validate_timeout(
            "call_timeout_seconds",
            call_timeout_seconds,
            maximum=DEFAULT_CALL_TIMEOUT_SECONDS,
        )
        if settings.provider_order != ("qwencloud", "openrouter"):
            raise QwenConfigurationError(
                "Qwen Cloud must be first and OpenRouter must be fallback-only"
            )

        validated_base_url: str | None
        try:
            validated_base_url = validate_qwen_base_url(settings.qwen_base_url)
        except (TypeError, ValueError):
            validated_base_url = None
        if validated_base_url is None:
            raise QwenConfigurationError(
                "QWEN_BASE_URL must be an approved Alibaba Model Studio endpoint"
            )

        self._settings = settings
        self._qwen_base_url = validated_base_url
        self._trail = trail
        self._logger = logger or _LOGGER
        self._attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._validate_models()
        self._http_client = http_client or httpx.AsyncClient(follow_redirects=False)
        self._owns_http_client = http_client is None

    async def __aenter__(self) -> "QwenClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        role: ModelRole | str = ModelRole.PRIMARY,
        tools: Sequence[Mapping[str, Any]] | None = None,
        thinking: bool = False,
        incident_id: str | None = None,
        trace_id: str | None = None,
    ) -> ChatCompletion:
        """Send one logical chat call across the accepted ordered Qwen route."""

        selected_role = ModelRole(role)
        if selected_role is ModelRole.FAST and thinking:
            raise ValueError("thinking mode is reserved for the primary reasoning role")
        if not messages or any(not isinstance(item, Mapping) for item in messages):
            raise ValueError("messages must contain at least one mapping")
        if tools is not None and any(not isinstance(item, Mapping) for item in tools):
            raise ValueError("tools must contain mappings")
        if self._trail is not None and not incident_id:
            raise ValueError("incident_id is required when a decision trail is configured")
        if self._trail is not None and not trace_id:
            raise ValueError("trace_id is required when a decision trail is configured")

        attempts = self._attempts(selected_role)
        progress = _RouteProgress()
        payload_base: dict[str, Any] = {
            "messages": copy.deepcopy(list(messages)),
        }
        if tools is not None:
            payload_base["tools"] = copy.deepcopy(list(tools))

        logical_timeout = False
        try:
            return await asyncio.wait_for(
                self._chat_across_route(
                    attempts,
                    payload_base,
                    thinking=thinking,
                    incident_id=incident_id,
                    trace_id=trace_id,
                    progress=progress,
                ),
                timeout=self._call_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logical_timeout = True

        # The asyncio deadline exception is no longer active here, so the
        # deliberately redacted public error cannot retain provider internals in
        # ``__context__``. The current route is a safe provider/model label only.
        if logical_timeout:
            active = progress.current or attempts[0]
            if progress.current is not None:
                self._record_terminal_attempt(
                    active,
                    outcome="failure",
                    reason="logical_timeout",
                    incident_id=incident_id,
                    trace_id=trace_id,
                )
            raise QwenExhaustedError(
                active.provider,
                active.model,
                "logical_timeout",
            )
        raise AssertionError("logical timeout handling unexpectedly fell through")

    async def _chat_across_route(
        self,
        attempts: tuple[_Attempt, ...],
        payload_base: dict[str, Any],
        *,
        thinking: bool,
        incident_id: str | None,
        trace_id: str | None,
        progress: _RouteProgress,
    ) -> ChatCompletion:
        for index, attempt in enumerate(attempts):
            progress.current = attempt
            self._require_credential(attempt)
            payload = copy.deepcopy(payload_base)
            payload["model"] = attempt.model
            if thinking:
                if attempt.provider == "qwencloud":
                    payload["enable_thinking"] = True
                else:
                    payload["reasoning"] = {"enabled": True}

            response: httpx.Response | None = None
            request_failure_reason: str | None = None
            request_failure_allows_fallback = False
            try:
                response = await asyncio.wait_for(
                    self._http_client.post(
                        attempt.endpoint,
                        headers={
                            "Authorization": f"Bearer {attempt.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=httpx.Timeout(self._attempt_timeout_seconds),
                    ),
                    timeout=self._attempt_timeout_seconds,
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                request_failure_reason = "timeout"
                request_failure_allows_fallback = True
            except httpx.RequestError:
                request_failure_reason = "transport_error"

            # Provider exceptions are no longer active here. Safe client errors
            # raised below therefore cannot retain response text in __context__.
            if request_failure_reason is not None:
                if request_failure_allows_fallback:
                    self._fallback_or_raise(
                        attempts,
                        index,
                        attempt,
                        request_failure_reason,
                        incident_id,
                        trace_id,
                    )
                    continue
                self._record_terminal_attempt(
                    attempt,
                    outcome="failure",
                    reason=request_failure_reason,
                    incident_id=incident_id,
                    trace_id=trace_id,
                )
                raise QwenCallError(
                    attempt.provider,
                    attempt.model,
                    request_failure_reason,
                )

            if response is None:
                raise AssertionError("provider request returned no response")

            status_code = response.status_code
            fallback_reason = _fallback_reason(response)
            if fallback_reason is not None:
                if self._fallback_or_raise(
                    attempts,
                    index,
                    attempt,
                    fallback_reason,
                    incident_id,
                    trace_id,
                ):
                    continue
                raise AssertionError("unreachable")
            if not 200 <= status_code < 300:
                reason = f"http_{status_code}"
                self._record_terminal_attempt(
                    attempt,
                    outcome="failure",
                    reason=reason,
                    incident_id=incident_id,
                    trace_id=trace_id,
                )
                raise QwenCallError(
                    attempt.provider,
                    attempt.model,
                    reason,
                )

            data: Any = _MISSING
            try:
                data = response.json()
            except (ValueError, RecursionError):
                pass
            if data is _MISSING:
                self._record_terminal_attempt(
                    attempt,
                    outcome="failure",
                    reason="invalid_json",
                    incident_id=incident_id,
                    trace_id=trace_id,
                )
                raise QwenCallError(
                    attempt.provider,
                    attempt.model,
                    "invalid_json",
                )
            try:
                completion = ChatCompletion.from_response(
                    attempt.provider,
                    attempt.model,
                    data,
                )
            except QwenCallError as exc:
                self._record_terminal_attempt(
                    attempt,
                    outcome="failure",
                    reason=exc.reason,
                    incident_id=incident_id,
                    trace_id=trace_id,
                )
                raise
            self._record_terminal_attempt(
                attempt,
                outcome="success",
                reason="success",
                incident_id=incident_id,
                trace_id=trace_id,
            )
            return completion

        raise AssertionError("configured Qwen route unexpectedly contained no attempts")

    @staticmethod
    def _validate_timeout(name: str, value: float, *, maximum: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        if value > maximum:
            raise ValueError(
                f"{name} cannot exceed the ADR-013 maximum of {maximum:g} seconds"
            )

    def _fallback_or_raise(
        self,
        attempts: tuple[_Attempt, ...],
        index: int,
        failed: _Attempt,
        reason: str,
        incident_id: str | None,
        trace_id: str | None,
    ) -> bool:
        next_index = index + 1
        if next_index >= len(attempts):
            self._record_terminal_attempt(
                failed,
                outcome="failure",
                reason=reason,
                incident_id=incident_id,
                trace_id=trace_id,
            )
            raise QwenExhaustedError(failed.provider, failed.model, reason)

        following = attempts[next_index]
        if not following.api_key:
            # The failed pair was attempted, but the next route is unusable and
            # therefore must not be represented as a transition that occurred.
            self._record_terminal_attempt(
                failed,
                outcome="failure",
                reason=reason,
                incident_id=incident_id,
                trace_id=trace_id,
            )
        self._require_credential(following)
        transition = {
            "from": failed.route,
            "to": following.route,
            "reason": reason,
        }
        if self._trail is not None:
            self._trail.append(
                incident_id or "",
                TrailEntryType.FALLBACK,
                transition,
                model_used=failed.model,
            )
        # The rendered message is deliberately allowlisted because the shared
        # JSON formatter exposes custom fields only through ``message`` today.
        # The same values remain separate LogRecord attributes for structured
        # log collectors. Never attach provider bodies or exception strings.
        self._logger.info(
            "qwen_provider_fallback from=%s to=%s reason=%s",
            transition["from"],
            transition["to"],
            transition["reason"],
            extra={
                "incident_id": incident_id or "-",
                "trace_id": trace_id or "-",
                "from": transition["from"],
                "to": transition["to"],
                "reason": transition["reason"],
            },
        )
        return True

    def _record_terminal_attempt(
        self,
        attempt: _Attempt,
        *,
        outcome: str,
        reason: str,
        incident_id: str | None,
        trace_id: str | None,
    ) -> None:
        """Append one allowlisted outcome for the attempt that ended the call."""

        if self._trail is None:
            return
        self._trail.append(
            incident_id or "",
            TrailEntryType.QWEN_ATTEMPT,
            {
                "provider": attempt.provider,
                "model": attempt.model,
                "outcome": outcome,
                "reason": reason,
                "trace_id": trace_id or "-",
            },
            model_used=attempt.model,
        )

    def _attempts(self, role: ModelRole) -> tuple[_Attempt, ...]:
        if role is ModelRole.FAST:
            models_by_provider = {
                "qwencloud": (self._settings.fast_model,),
                "openrouter": (self._settings.openrouter_fast_model,),
            }
        else:
            models_by_provider = {
                "qwencloud": self._settings.qwencloud_models,
                "openrouter": self._settings.openrouter_models,
            }
        base_urls = {
            "qwencloud": self._qwen_base_url,
            "openrouter": OPENROUTER_BASE_URL,
        }
        api_keys = {
            "qwencloud": self._settings.dashscope_api_key,
            "openrouter": self._settings.openrouter_api_key,
        }
        return tuple(
            _Attempt(provider, model, base_urls[provider], api_keys[provider])
            for provider in self._settings.provider_order
            for model in models_by_provider[provider]
        )

    def _validate_models(self) -> None:
        if self._settings.qwencloud_models != RUNTIME_QWENCLOUD_MODELS:
            raise QwenConfigurationError(
                "Qwen Cloud reasoning chain must exactly match the post-M0 route"
            )
        if self._settings.openrouter_models != RUNTIME_OPENROUTER_MODELS:
            raise QwenConfigurationError(
                "OpenRouter reasoning chain must exactly match the post-M0 route"
            )
        if self._settings.primary_model != RUNTIME_QWENCLOUD_MODELS[0]:
            raise QwenConfigurationError(
                "PRIMARY_MODEL must remain qwen3.7-max after M0"
            )
        if self._settings.fast_model != "qwen-flash":
            raise QwenConfigurationError("Qwen Cloud fast model must remain qwen-flash")
        if self._settings.openrouter_fast_model != DEFAULT_OPENROUTER_FAST_MODEL:
            raise QwenConfigurationError(
                "OpenRouter fast model must remain qwen/qwen3.6-flash"
            )

    @staticmethod
    def _require_credential(attempt: _Attempt) -> None:
        if not attempt.api_key:
            raise QwenConfigurationError(
                f"{attempt.provider} credential is required for its configured route"
            )


def _is_fallback_status(status_code: int) -> bool:
    return status_code in _FALLBACK_STATUS_CODES or 500 <= status_code <= 599


def _fallback_reason(response: httpx.Response) -> str | None:
    status_code = response.status_code
    if _is_fallback_status(status_code):
        return f"http_{status_code}"
    if status_code == 400 and _is_explicit_model_unavailable(response):
        return "model_unavailable"
    return None


def _is_explicit_model_unavailable(response: httpx.Response) -> bool:
    """Recognize only bounded, explicit model-routing errors from HTTP 400."""

    try:
        content = response.content
    except (httpx.ResponseNotRead, RuntimeError):
        return False
    if len(content) > _MAX_FALLBACK_ERROR_BYTES:
        return False

    try:
        payload = response.json()
    except (TypeError, ValueError, RecursionError):
        return False
    if not isinstance(payload, Mapping):
        return False

    envelopes: list[Mapping[str, Any]] = [payload]
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        envelopes.append(nested)

    for envelope in envelopes:
        code = envelope.get("code")
        if isinstance(code, str) and len(code) <= 128:
            normalized = re.sub(r"[^a-z0-9]+", "_", code.casefold()).strip("_")
            if normalized in _MODEL_UNAVAILABLE_CODES:
                return True

    messages: list[str] = []
    if isinstance(nested, str):
        messages.append(nested)
    for envelope in envelopes:
        for key in ("message", "detail"):
            value = envelope.get(key)
            if isinstance(value, str):
                messages.append(value)
    return any(
        len(message) <= _MAX_FALLBACK_ERROR_TEXT_CHARS
        and any(pattern.search(message) for pattern in _MODEL_UNAVAILABLE_MESSAGES)
        for message in messages
    )
