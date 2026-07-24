"""Bounded, fail-open content capture with conservative secret redaction."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any

from .config import Settings

CONTENT_DISABLED = "[content capture disabled]"
_OMITTED = "[omitted]"
_SENSITIVE_KEYS = re.compile(
    r"^(?:"
    r"api[-_]?key|authorization|proxy[-_]?authorization|"
    r"access[-_]?token|refresh[-_]?token|auth[-_]?token|"
    r"secret|client[-_]?secret|password|passwd|"
    r"cookie|set[-_]?cookie|credential|private[-_]?key"
    r")$",
    re.IGNORECASE,
)
_REASONING_KEYS = re.compile(
    r"^(?:"
    r"reasoning(?:[-_]?(?:content|details|items|summary))?|"
    r"thinking(?:[-_]?(?:content|details|blocks))?|"
    r"codex[-_]?reasoning[-_]?(?:content|items)|"
    r"thought[-_]?signature|"
    r"chain[-_]?of[-_]?thought|analysis|"
    r"encrypted[-_]?(?:content|reasoning)"
    r")$",
    re.IGNORECASE,
)
_REASONING_BLOCK_TYPES = frozenset(
    {
        "thinking",
        "redacted_thinking",
        "reasoning",
        "reasoning_content",
        "chain_of_thought",
    }
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_COMMON_SECRET = re.compile(r"(?i)\b(?:sk|rk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_CLOUD_ACCESS_KEY = re.compile(r"\b(?:(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{20,})\b")
_DATA_URI = re.compile(
    r"(?i)data:[^,\s\"']{0,240};base64,[A-Za-z0-9+/=_-]+",
)
_COOKIE_HEADER = re.compile(r"(?im)\b(cookie|set-cookie)\s*:[^\r\n]*")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?-----END \1-----",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(api[-_]?key|access[-_]?token|password|secret)\s*[:=]\s*"
    r"([\"']?)[^\s,\"'}]{6,}\2"
)
_QUOTED_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(api[-_]?key|access[-_]?token|password|secret)\s*[:=]\s*"
    r"([\"'])[^\"'\r\n]{1,4096}\2"
)


def _redact_string(value: str) -> str:
    value = _DATA_URI.sub(
        lambda match: f"[data URI omitted: {match.group(0).split(',', 1)[0][:120]}]",
        value,
    )
    value = _PEM_PRIVATE_KEY.sub("[PRIVATE KEY REDACTED]", value)
    value = _COOKIE_HEADER.sub(lambda match: f"{match.group(1)}: [REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _JWT.sub("[REDACTED]", value)
    value = _CLOUD_ACCESS_KEY.sub("[REDACTED]", value)
    value = _COMMON_SECRET.sub("[REDACTED]", value)
    value = _QUOTED_ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    return _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


def _normalize(
    value: Any,
    *,
    max_items: int,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    if depth >= 10:
        return "[maximum depth reached]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[binary omitted: {len(value)} bytes]"

    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "[recursive value]"
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            item_count = len(value)
            items = list(islice(value.items(), max_items))
            block_type = str(value.get("type", "")).strip().lower().replace("-", "_")
            if block_type in _REASONING_BLOCK_TYPES:
                for key, item in items[:max_items]:
                    text_key = str(key)
                    normalized[text_key] = (
                        _redact_string(str(item))
                        if text_key.lower() == "type"
                        else "[REDACTED REASONING]"
                    )
                if item_count > max_items:
                    normalized["_hermes_galileo_omitted_items"] = item_count - max_items
                return normalized
            for key, item in items:
                text_key = str(key)
                if _REASONING_KEYS.match(text_key):
                    normalized[text_key] = "[REDACTED REASONING]"
                elif _SENSITIVE_KEYS.match(text_key):
                    normalized[text_key] = "[REDACTED]"
                else:
                    normalized[text_key] = _normalize(
                        item,
                        max_items=max_items,
                        depth=depth + 1,
                        seen=seen,
                    )
            if item_count > max_items:
                normalized["_hermes_galileo_omitted_items"] = item_count - max_items
            return normalized

        if isinstance(value, Sequence):
            item_count = len(value)
            items = list(islice(value, max_items))
            result = [
                _normalize(item, max_items=max_items, depth=depth + 1, seen=seen) for item in items
            ]
            if item_count > max_items:
                result.append(f"[{item_count - max_items} items omitted]")
            return result

        if hasattr(value, "model_dump"):
            return _normalize(
                value.model_dump(mode="json"),
                max_items=max_items,
                depth=depth + 1,
                seen=seen,
            )
        if hasattr(value, "__dict__"):
            return _normalize(
                vars(value),
                max_items=max_items,
                depth=depth + 1,
                seen=seen,
            )
        return _redact_string(str(value))
    except Exception:
        return _OMITTED
    finally:
        seen.discard(identity)


def clip_text(value: str, max_chars: int) -> str:
    """Clip a value to a hard bound while retaining its original length."""

    if len(value) <= max_chars:
        return value
    suffix = f"… [truncated; original chars={len(value)}]"
    if len(suffix) >= max_chars:
        return suffix[:max_chars]
    return value[: max_chars - len(suffix)] + suffix


def captured_value(value: Any, settings: Settings) -> str:
    """Return a redacted, serialized, bounded value or the privacy placeholder."""

    if not settings.capture_content:
        return CONTENT_DISABLED
    normalized = _normalize(value, max_items=settings.max_collection_items)
    if isinstance(normalized, str):
        serialized = normalized
    else:
        try:
            serialized = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except Exception:
            serialized = _OMITTED
    return clip_text(serialized, settings.max_content_chars)


def messages_json(role: str, value: Any, settings: Settings) -> str:
    """Serialize one message in the format Galileo's OTLP provider expects."""

    content = captured_value(value, settings)
    return json.dumps([{"role": role, "content": content}], ensure_ascii=False)


def _bounded_messages_json(
    messages: Any,
    settings: Settings,
    *,
    fallback_role: str,
) -> str:
    """Serialize structured messages without ever returning truncated JSON."""

    normalized = _normalize(messages, max_items=settings.max_collection_items)
    try:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        return messages_json(fallback_role, _OMITTED, settings)
    if len(serialized) <= settings.max_content_chars:
        return serialized
    marker = f"[messages omitted; serialized chars={len(serialized)}]"
    return json.dumps(
        [{"role": fallback_role, "content": marker}],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def request_messages_json(request: Any, settings: Settings) -> str:
    """Extract the canonical Hermes request messages with privacy controls.

    ``hermes.observer.v1`` exposes the sanitized provider envelope as
    ``request["body"]``. Direct ``messages``/``input`` keys remain supported
    for older Hermes payloads.
    """

    if not settings.capture_content:
        return messages_json("user", CONTENT_DISABLED, settings)
    if isinstance(request, Mapping):
        body = request.get("body")
        source = body if isinstance(body, Mapping) else request
        messages = source.get("messages") or source.get("input")
        if messages:
            return _bounded_messages_json(messages, settings, fallback_role="user")
    return messages_json("user", request, settings)


def response_messages_json(response: Any, settings: Settings) -> str:
    """Format a response as an assistant message for Galileo."""

    if isinstance(response, Mapping):
        assistant_message = response.get("assistant_message")
        if isinstance(assistant_message, Mapping):
            if not settings.capture_content:
                return messages_json("assistant", CONTENT_DISABLED, settings)
            return _bounded_messages_json(
                [assistant_message],
                settings,
                fallback_role="assistant",
            )
        if "role" in response and ("content" in response or "tool_calls" in response):
            if not settings.capture_content:
                return messages_json("assistant", CONTENT_DISABLED, settings)
            return _bounded_messages_json(
                [response],
                settings,
                fallback_role="assistant",
            )
        for key in ("content", "output_text", "text", "message"):
            if response.get(key) not in (None, ""):
                return messages_json("assistant", response[key], settings)
    return messages_json("assistant", response, settings)


def anonymize_identifier(value: Any, *, enabled: bool, secret: str = "") -> str:
    """Return a stable pseudonym unless raw identifiers were explicitly enabled."""

    text = str(value or "").strip()
    if not text:
        return ""
    if not enabled:
        return text
    encoded = text.encode("utf-8", errors="replace")
    if secret:
        digest = hmac.new(
            secret.encode("utf-8", errors="replace"),
            encoded,
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest[:24]}"
    digest = hashlib.sha256(encoded).hexdigest()
    return f"sha256:{digest[:24]}"
