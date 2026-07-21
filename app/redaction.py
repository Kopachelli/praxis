"""Bounded, shared redaction for untrusted alert and tool evidence [NFR-5]."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

# No single text value can be larger than the accepted webhook request body.
# Keeping a hard scan ceiling also makes the regex work deterministic for tool
# adapters added later, even if their upstream source is not the webhook body.
MAX_REDACTION_SCAN_CHARS = 262_144
MAX_REDACTION_DEPTH = 8
MAX_REDACTION_ITEMS = 64
MAX_TOOL_EVIDENCE_TEXT_CHARS = 2_000

MAX_PUBLIC_SOURCE_CHARS = 120
MAX_PUBLIC_SERVICE_CHARS = 128
MAX_PUBLIC_SIGNAL_CHARS = 160
MAX_PUBLIC_TITLE_CHARS = 1_000

MAX_SENSITIVE_KEY_CHARS = 128
MAX_SENSITIVE_KEY_SEGMENTS = 16

_ASSIGNMENT_START = re.compile(
    r"(?ix)"
    r"(?<![\w-])"
    r"(?P<key_quote>[\"']?)"
    r"(?P<key>[a-z0-9][a-z0-9_-]{0,127})"
    r"(?P=key_quote)"
    r"\s*[:=]\s*"
)
_QUOTED_ASSIGNMENT_START = re.compile(
    r"(?P<key_delimiter>\\?[\"'])"
    r"(?P<key>(?:\\+[uU][0-9a-fA-F]{4}|[A-Za-z0-9_-])+?)"
    r"(?P=key_delimiter)"
    r"\s*:\s*"
    r"(?P<value_delimiter>\\?[\"'])"
)
_UNICODE_KEY_ESCAPE = re.compile(r"\\+[uU]([0-9a-fA-F]{4})")
_CAMEL_KEY_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "apikey",
        "accesskey",
        "clientsecret",
        "secretkey",
        "signingkey",
        "secret",
        "password",
        "passwd",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "token",
        "setcookie",
        "cookie",
        "credential",
        "privatekey",
    }
)
_SENSITIVE_KEY_SUFFIXES = frozenset(
    {
        ("api", "key"),
        ("api", "key", "id"),
        ("access", "key"),
        ("access", "key", "id"),
        ("client", "secret"),
        ("secret", "key"),
        ("signing", "key"),
        ("access", "token"),
        ("refresh", "token"),
        ("id", "token"),
        ("session", "token"),
        ("set", "cookie"),
        ("private", "key"),
        *((key,) for key in _SENSITIVE_KEYS),
    }
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]+")
_JWT_LIKE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*"
    r"(?![A-Za-z0-9_-])"
)
_URL_AUTHORITY = re.compile(
    r"(?i)\b"
    r"(?P<scheme>[a-z][a-z0-9+.-]{1,31})"
    r"(?P<separator>://|:\\/\\/)"
    r"(?P<authority>[^\s/?#<>\"']+)"
)


def redact_text(value: Any, *, max_chars: int) -> str | None:
    """Return bounded useful text with common credential forms removed."""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(value, str):
        return None
    original = value.strip()
    if not original:
        return None

    scan_was_truncated = len(original) > MAX_REDACTION_SCAN_CHARS
    text = original[:MAX_REDACTION_SCAN_CHARS]
    text = _URL_AUTHORITY.sub(_redact_url_authority, text)
    text = _redact_quoted_assignments(text)
    text = _redact_assignments(text)
    text = _BEARER_TOKEN.sub(f"Bearer {REDACTED}", text)
    text = _JWT_LIKE_TOKEN.sub(REDACTED, text)

    if scan_was_truncated or len(text) > max_chars:
        return f"{text[:max_chars]}…"
    return text


def redact_structure(
    value: Any,
    *,
    max_depth: int = MAX_REDACTION_DEPTH,
    max_items: int = MAX_REDACTION_ITEMS,
    max_string_chars: int = MAX_TOOL_EVIDENCE_TEXT_CHARS,
) -> Any:
    """Recursively sanitize and bound JSON-like tool evidence."""

    if (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or max_depth < 0
        or not isinstance(max_items, int)
        or isinstance(max_items, bool)
        or max_items <= 0
    ):
        raise ValueError("redaction structure bounds are invalid")

    def walk(item: Any, depth: int) -> Any:
        if depth > max_depth:
            return TRUNCATED
        if isinstance(item, str):
            return redact_text(item, max_chars=max_string_chars) or ""
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            truncated = False
            for index, (raw_key, nested) in enumerate(item.items()):
                if index >= max_items:
                    truncated = True
                    break
                key = redact_text(str(raw_key), max_chars=128) or "<empty-key>"
                result[key] = (
                    REDACTED
                    if _is_sensitive_key(str(raw_key))
                    else walk(nested, depth + 1)
                )
            if truncated:
                result["_praxis_truncated"] = True
            return result
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            values = [walk(nested, depth + 1) for nested in item[:max_items]]
            if len(item) > max_items:
                values.append(TRUNCATED)
            return values
        return REDACTED

    return walk(value, 0)


def _redact_url_authority(match: re.Match[str]) -> str:
    authority = match.group("authority")
    if "@" not in authority:
        return match.group(0)
    _, host = authority.rsplit("@", 1)
    return (
        f"{match.group('scheme')}{match.group('separator')}"
        f"{REDACTED}@{host}"
    )


def _redact_quoted_assignments(text: str) -> str:
    """Redact JSON strings, including common escaped-JSON log envelopes."""

    chunks: list[str] = []
    copied_to = 0
    search_from = 0
    while match := _QUOTED_ASSIGNMENT_START.search(text, search_from):
        search_from = match.end()
        key = _UNICODE_KEY_ESCAPE.sub(
            lambda escape: chr(int(escape.group(1), 16)),
            match.group("key"),
        )
        if not _is_sensitive_key(key):
            continue

        delimiter = match.group("value_delimiter")
        value_end = _quoted_value_end(text, match.end(), delimiter)
        if value_end is None:
            # Once a sensitive quoted value starts, an absent terminator leaves
            # no trustworthy boundary between credential material and the rest
            # of the malformed envelope. Fail closed by consuming the suffix
            # and synthesize the matching delimiter so later redaction passes
            # cannot reinterpret any of that suffix as safe text.
            chunks.extend((text[copied_to : match.end()], REDACTED, delimiter))
            copied_to = len(text)
            break

        chunks.extend((text[copied_to : match.end()], REDACTED, delimiter))
        copied_to = value_end + len(delimiter)
        search_from = copied_to

    if not chunks:
        return text
    chunks.append(text[copied_to:])
    return "".join(chunks)


def _redact_assignments(text: str) -> str:
    """Redact bounded env/header-style assignments with sensitive key suffixes."""

    chunks: list[str] = []
    copied_to = 0
    search_from = 0
    while match := _ASSIGNMENT_START.search(text, search_from):
        value_start = match.end()
        search_from = value_start
        if not _is_sensitive_key(match.group("key")):
            continue

        value = _assignment_value(
            text,
            value_start,
            redact_entire_value=_is_authorization_key(match.group("key")),
        )
        if value is None:
            continue
        value_end, replacement = value
        chunks.extend((text[copied_to:value_start], replacement))
        copied_to = value_end
        search_from = value_end

    if not chunks:
        return text
    chunks.append(text[copied_to:])
    return "".join(chunks)


def _assignment_value(
    text: str,
    start: int,
    *,
    redact_entire_value: bool = False,
) -> tuple[int, str] | None:
    if start >= len(text):
        return None

    # Authorization is the complete sensitive header boundary, not a parsed
    # scheme/token pair. Consume through the physical CR/LF boundary, including
    # folded continuation lines, so parameters after whitespace, quotes, commas,
    # or semicolons cannot survive for either known or vendor-specific schemes.
    if redact_entire_value:
        return _header_value_end(
            text,
            start,
            stop_at_semicolon=False,
        ), REDACTED

    quote = text[start]
    if quote in ('"', "'"):
        value_end = _quoted_value_end(text, start + 1, quote)
        if value_end is None:
            # A malformed quoted env/header assignment has no safe recovery
            # boundary. Redact through the bounded scan suffix rather than
            # allowing the unterminated value to pass through unchanged.
            return len(text), f"{quote}{REDACTED}{quote}"
        return value_end + 1, f"{quote}{REDACTED}{quote}"

    bearer = _BEARER_TOKEN.match(text, start)
    if bearer is not None:
        return bearer.end(), REDACTED

    value = re.match(r"[^\s,;]+", text[start:])
    if value is None:
        return None
    return start + value.end(), REDACTED


def _header_value_end(
    text: str,
    start: int,
    *,
    stop_at_semicolon: bool = True,
) -> int:
    """Return a multipart auth value end without splitting quoted parameters."""

    quote: str | None = None
    escaped = False
    index = start
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in ('"', "'"):
            quote = character
            index += 1
            continue
        if stop_at_semicolon and character == ";":
            return index
        if character in "\r\n":
            next_line = index + 1
            if character == "\r" and next_line < len(text) and text[next_line] == "\n":
                next_line += 1
            if next_line < len(text) and text[next_line] in " \t":
                index = next_line + 1
                continue
            return index
        index += 1
    return len(text)


def _quoted_value_end(text: str, start: int, delimiter: str) -> int | None:
    quote = delimiter[-1]
    index = start
    if len(delimiter) == 1:
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                return index
            index += 1
        return None

    while index < len(text):
        if text[index] != "\\":
            index += 1
            continue
        run_start = index
        while index < len(text) and text[index] == "\\":
            index += 1
        if index < len(text) and text[index] == quote:
            if index - run_start == 1:
                return run_start
            index += 1
    return None


def _is_sensitive_key(value: str) -> bool:
    decoded = _UNICODE_KEY_ESCAPE.sub(
        lambda escape: chr(int(escape.group(1), 16)),
        value,
    )
    normalized = re.sub(r"[^a-z0-9]", "", decoded.casefold())
    if normalized in _SENSITIVE_KEYS:
        return True
    if len(decoded) > MAX_SENSITIVE_KEY_CHARS:
        return False

    segmented = _CAMEL_KEY_BOUNDARY.sub("_", decoded)
    segments = tuple(re.findall(r"[a-z0-9]+", segmented.casefold()))
    if not segments or len(segments) > MAX_SENSITIVE_KEY_SEGMENTS:
        return False
    return any(
        len(segments) >= len(suffix) and segments[-len(suffix) :] == suffix
        for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _is_authorization_key(value: str) -> bool:
    decoded = _UNICODE_KEY_ESCAPE.sub(
        lambda escape: chr(int(escape.group(1), 16)),
        value,
    )
    segmented = _CAMEL_KEY_BOUNDARY.sub("_", decoded)
    segments = tuple(re.findall(r"[a-z0-9]+", segmented.casefold()))
    return bool(segments) and segments[-1] == "authorization"
