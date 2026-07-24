"""Unit tests for privacy-preserving content normalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from hermes_galileo.config import Settings
from hermes_galileo.privacy import (
    CONTENT_DISABLED,
    anonymize_identifier,
    captured_value,
    clip_text,
    messages_json,
    request_messages_json,
    response_messages_json,
)


def _settings(**overrides: Any) -> Settings:
    return replace(Settings.from_env({}), **overrides)


def test_content_capture_is_opt_in() -> None:
    private_prompt = "customer account 12345"

    disabled = captured_value(private_prompt, _settings())
    enabled = captured_value(private_prompt, _settings(capture_content=True))

    assert disabled == CONTENT_DISABLED
    assert private_prompt not in disabled
    assert enabled == private_prompt


def test_sensitive_mapping_keys_are_redacted_recursively() -> None:
    payload = {
        "safe": "visible",
        "authorization": "Bearer outer-secret-token",
        "nested": [
            {
                "apiKey": "nested-api-secret",
                "deeper": {
                    "client_secret": "client-secret-value",
                    "password": "password-value",
                },
            }
        ],
    }

    serialized = captured_value(payload, _settings(capture_content=True))
    normalized = json.loads(serialized)

    assert normalized == {
        "authorization": "[REDACTED]",
        "nested": [
            {
                "apiKey": "[REDACTED]",
                "deeper": {
                    "client_secret": "[REDACTED]",
                    "password": "[REDACTED]",
                },
            }
        ],
        "safe": "visible",
    }
    for secret in (
        "outer-secret-token",
        "nested-api-secret",
        "client-secret-value",
        "password-value",
    ):
        assert secret not in serialized


def test_hidden_reasoning_fields_are_always_redacted() -> None:
    settings = _settings(capture_content=True, capture_conversation_history=True)
    payload = {
        "role": "assistant",
        "reasoning_content": "private reasoning one",
        "reasoningDetails": {"thinking": "private reasoning two"},
        "codex_reasoning_items": [
            {"encrypted_content": "private reasoning three"},
        ],
        "chainOfThought": "private reasoning four",
        "content": [
            {"type": "text", "text": "public block"},
            {
                "type": "thinking",
                "thinking": "private reasoning five",
                "signature": "anthropic-signature-secret",
            },
            {
                "type": "redacted_thinking",
                "data": "encrypted-reasoning-secret",
            },
        ],
        "extra_content": {
            "google": {"thought_signature": "gemini-signature-secret"},
        },
    }

    serialized = captured_value(payload, settings)
    normalized = json.loads(serialized)

    assert normalized["reasoning_content"] == "[REDACTED REASONING]"
    assert normalized["reasoningDetails"] == "[REDACTED REASONING]"
    assert normalized["codex_reasoning_items"] == "[REDACTED REASONING]"
    assert normalized["chainOfThought"] == "[REDACTED REASONING]"
    assert normalized["content"][0] == {"type": "text", "text": "public block"}
    assert normalized["content"][1] == {
        "type": "thinking",
        "thinking": "[REDACTED REASONING]",
        "signature": "[REDACTED REASONING]",
    }
    assert normalized["content"][2] == {
        "type": "redacted_thinking",
        "data": "[REDACTED REASONING]",
    }
    assert normalized["extra_content"]["google"]["thought_signature"] == ("[REDACTED REASONING]")
    assert "private reasoning" not in serialized
    assert "signature-secret" not in serialized
    assert "encrypted-reasoning-secret" not in serialized


def test_recursive_values_are_replaced_instead_of_recursing_forever() -> None:
    recursive: dict[str, object] = {"safe": "visible"}
    recursive["self"] = recursive

    normalized = json.loads(captured_value(recursive, _settings(capture_content=True)))

    assert normalized == {"safe": "visible", "self": "[recursive value]"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "Authorization: Bearer abcDEF0123456789._~-",
            "Authorization: Bearer [REDACTED]",
        ),
        (
            "token=sk_live_abcdefghijklmnop",
            "token=[REDACTED]",
        ),
        (
            "api_key='abcdefghijklmnop'",
            "api_key=[REDACTED]",
        ),
        (
            'password: "correct horse battery staple"',
            "password=[REDACTED]",
        ),
        (
            "jwt=eyJhbGciOiJIUzI1NiJ9.c3ludGhldGljLXBheWxvYWQ.c3ludGhldGljLXNpZ25hdHVyZQ",
            "jwt=[REDACTED]",
        ),
        (
            "aws=AKIAIOSFODNN7EXAMPLE",
            "aws=[REDACTED]",
        ),
        (
            "google=AIzaSySyntheticApiKeyMaterial123456",
            "google=[REDACTED]",
        ),
        (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
            "[data URI omitted: data:image/png;base64]",
        ),
        (
            'error: image="data:image/png;base64,inline-base64-secret-0123456789" safe',
            'error: image="[data URI omitted: data:image/png;base64]" safe',
        ),
        (
            "Cookie: session_id=top-secret; csrftoken=also-secret",
            "Cookie: [REDACTED]",
        ),
        (
            "request headers\nSet-Cookie: session_id=top-secret; Secure\nsafe",
            "request headers\nSet-Cookie: [REDACTED]\nsafe",
        ),
        (
            "key:\n-----BEGIN PRIVATE KEY-----\nprivate-key-material\n"
            "-----END PRIVATE KEY-----\nsafe",
            "key:\n[PRIVATE KEY REDACTED]\nsafe",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nrsa-private-key-material\n"
            "-----END RSA PRIVATE KEY-----",
            "[PRIVATE KEY REDACTED]",
        ),
    ],
)
def test_secrets_and_data_uris_in_text_are_redacted(
    value: str,
    expected: str,
) -> None:
    assert captured_value(value, _settings(capture_content=True)) == expected


def test_message_helpers_preserve_privacy_and_roles() -> None:
    disabled_request = json.loads(
        request_messages_json(
            {"messages": [{"role": "user", "content": "private"}]},
            _settings(),
        )
    )
    enabled_request = json.loads(
        request_messages_json(
            {"messages": [{"role": "user", "content": "hello"}]},
            _settings(capture_content=True),
        )
    )
    response = json.loads(
        response_messages_json(
            {"output_text": "world"},
            _settings(capture_content=True),
        )
    )

    assert disabled_request == [{"role": "user", "content": CONTENT_DISABLED}]
    assert enabled_request == [{"content": "hello", "role": "user"}]
    assert response == [{"role": "assistant", "content": "world"}]


def test_request_messages_extracts_canonical_hermes_envelope() -> None:
    request = {
        "method": "POST",
        "url": "https://provider.example/v1/chat",
        "body": {
            "messages": [{"role": "system", "content": "policy"}],
            "api_key": "[REDACTED]",
        },
    }

    messages = json.loads(request_messages_json(request, _settings(capture_content=True)))

    assert messages == [{"content": "policy", "role": "system"}]


def test_response_messages_extracts_canonical_hermes_envelope() -> None:
    response = {
        "model": "model-version",
        "finish_reason": "tool_calls",
        "assistant_message": {
            "role": "assistant",
            "content": "I will search.",
            "tool_calls": [{"id": "call-1", "function": {"name": "search"}}],
        },
    }

    messages = json.loads(response_messages_json(response, _settings(capture_content=True)))

    assert messages == [response["assistant_message"]]


def test_large_structured_messages_remain_valid_json() -> None:
    settings = _settings(capture_content=True, max_content_chars=256)
    request = {
        "body": {
            "messages": [{"role": "user", "content": "x" * 2_000}],
        }
    }

    serialized = request_messages_json(request, settings)

    assert len(serialized) <= 256
    assert json.loads(serialized) == [
        {
            "role": "user",
            "content": "[messages omitted; serialized chars=2030]",
        }
    ]


def test_messages_json_wraps_scalar_content_with_requested_role() -> None:
    result = json.loads(messages_json("tool", "tool output", _settings(capture_content=True)))

    assert result == [{"role": "tool", "content": "tool output"}]


@pytest.mark.parametrize("max_chars", [1, 8, 48])
def test_clip_text_never_exceeds_hard_bound(max_chars: int) -> None:
    clipped = clip_text("x" * 100, max_chars)

    assert len(clipped) == max_chars


def test_clip_text_retains_length_metadata_when_space_allows() -> None:
    clipped = clip_text("x" * 100, 48)

    assert clipped.startswith("x")
    assert clipped.endswith("… [truncated; original chars=100]")


def test_capture_truncates_collections_before_serialization() -> None:
    settings = _settings(capture_content=True, max_collection_items=2)

    mapping = json.loads(captured_value({"a": 1, "b": 2, "c": 3}, settings))
    sequence = json.loads(captured_value([1, 2, 3, 4], settings))

    assert mapping == {
        "a": 1,
        "b": 2,
        "_hermes_galileo_omitted_items": 1,
    }
    assert sequence == [1, 2, "[2 items omitted]"]


def test_collection_limit_bounds_iteration_work() -> None:
    class HugeSequence(Sequence[int]):
        reads = 0

        def __len__(self) -> int:
            return 1_000_000

        def __getitem__(self, index: int) -> int:
            if index >= len(self):
                raise IndexError
            self.reads += 1
            return index

    value = HugeSequence()
    normalized = json.loads(
        captured_value(
            value,
            _settings(capture_content=True, max_collection_items=2),
        )
    )

    assert normalized == [0, 1, "[999998 items omitted]"]
    assert value.reads == 2


def test_captured_value_applies_character_limit_after_redaction() -> None:
    settings = _settings(capture_content=True, max_content_chars=64)
    serialized = captured_value("visible-" + ("x" * 100), settings)

    assert len(serialized) == 64
    assert serialized.endswith("… [truncated; original chars=108]")


def test_identifier_hash_is_stable_bounded_and_does_not_expose_input() -> None:
    identifier = " user@example.test "
    expected_digest = hashlib.sha256(b"user@example.test").hexdigest()[:24]

    first = anonymize_identifier(identifier, enabled=True)
    second = anonymize_identifier(identifier, enabled=True)

    assert first == second == f"sha256:{expected_digest}"
    assert "user@example.test" not in first
    assert len(first) == 31


def test_identifier_hash_can_be_disabled_and_empty_values_stay_empty() -> None:
    assert anonymize_identifier(" raw-user-id ", enabled=False) == "raw-user-id"
    assert anonymize_identifier("", enabled=True) == ""
    assert anonymize_identifier(None, enabled=True) == ""


def test_identifier_hash_uses_keyed_hmac_when_secret_is_available() -> None:
    first = anonymize_identifier("user-42", enabled=True, secret="key-one")
    second = anonymize_identifier("user-42", enabled=True, secret="key-two")

    assert first.startswith("hmac-sha256:")
    assert second.startswith("hmac-sha256:")
    assert first != second


def test_binary_deep_and_object_values_are_normalized_safely() -> None:
    settings = _settings(capture_content=True)

    class ModelValue:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {"kind": "model", "authorization": "secret"}

    class PlainValue:
        def __init__(self) -> None:
            self.kind = "plain"

    class OpaqueValue:
        __slots__ = ()

        def __str__(self) -> str:
            return "opaque"

    class BrokenValue:
        __slots__ = ()

        def __str__(self) -> str:
            raise RuntimeError("cannot serialize")

    nested: Any = "leaf"
    for _ in range(11):
        nested = [nested]

    assert captured_value(b"\x00\x01", settings) == "[binary omitted: 2 bytes]"
    assert "[maximum depth reached]" in captured_value(nested, settings)
    assert json.loads(captured_value(ModelValue(), settings)) == {
        "authorization": "[REDACTED]",
        "kind": "model",
    }
    assert json.loads(captured_value(PlainValue(), settings)) == {"kind": "plain"}
    assert captured_value(OpaqueValue(), settings) == "opaque"
    assert captured_value(BrokenValue(), settings) == "[omitted]"


def test_legacy_request_and_direct_assistant_message_shapes() -> None:
    enabled = _settings(capture_content=True)
    disabled = _settings()

    request = json.loads(
        request_messages_json(
            {"input": [{"role": "user", "content": "legacy"}]},
            enabled,
        )
    )
    direct = {"role": "assistant", "content": "direct"}
    enabled_response = json.loads(response_messages_json(direct, enabled))
    disabled_response = json.loads(response_messages_json(direct, disabled))

    assert request == [{"content": "legacy", "role": "user"}]
    assert enabled_response == [direct]
    assert disabled_response == [{"content": CONTENT_DISABLED, "role": "assistant"}]
