"""Wire-level E2E: Hermes hooks → official Galileo SDK → OTLP/HTTP."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from galileo.config import GalileoPythonConfig
from opentelemetry.exporter.otlp.proto.http import trace_exporter as otlp_http_exporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

import hermes_galileo
from hermes_galileo import hooks

_PROJECT_ID = "10000000-0000-4000-8000-000000000001"
_LOG_STREAM_ID = "20000000-0000-4000-8000-000000000002"
_SESSION_ID = "30000000-0000-4000-8000-000000000003"
_NOW = "2026-01-01T00:00:00+00:00"


class _GalileoStub(ThreadingHTTPServer):
    requests: list[dict[str, Any]]

    def __init__(self, trace_actions: list[dict[str, Any]] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _GalileoHandler)
        self.requests = []
        self.trace_actions = list(trace_actions or [])
        self.sessions_by_external_id: dict[str, str] = {}


class _GalileoHandler(BaseHTTPRequestHandler):
    server: _GalileoStub

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _record(self, body: bytes = b"") -> None:
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )

    def _respond_json(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload).encode()
        self._respond_bytes(encoded, status=status, content_type="application/json")

    def _respond_bytes(
        self,
        payload: bytes,
        *,
        status: int,
        content_type: str = "application/x-protobuf",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        with suppress(OSError):
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._record()
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in {"/healthcheck", "/ingest/healthz"}:
            self._respond_json({"status": "ok"})
        elif path == "/current_user":
            self._respond_json(
                {
                    "id": "9b2c0b9a-5238-4f1d-8a2f-6cb1fb43e6f5",
                    "email": "e2e@example.test",
                    "role": "user",
                }
            )
        elif path == "/projects":
            self._respond_json(
                [
                    {
                        "id": _PROJECT_ID,
                        "created_by": "e2e-user",
                        "created_by_user": {
                            "id": "e2e-user",
                            "email": "e2e@example.test",
                        },
                        "runs": [],
                        "created_at": _NOW,
                        "updated_at": _NOW,
                        "permissions": [],
                        "name": "hermes-native-e2e-project",
                        "type": "gen_ai",
                    }
                ]
            )
        elif path == f"/projects/{_PROJECT_ID}/log_streams/paginated":
            self._respond_json(
                {
                    "log_streams": [
                        {
                            "id": _LOG_STREAM_ID,
                            "created_at": _NOW,
                            "updated_at": _NOW,
                            "name": "hermes-native-e2e-stream",
                            "project_id": _PROJECT_ID,
                            "created_by": "e2e-user",
                        }
                    ],
                    "starting_token": 0,
                    "limit": 500,
                    "paginated": False,
                    "next_starting_token": None,
                }
            )
        else:
            self._respond_json({"detail": "not found"}, status=404)

    def do_POST(self) -> None:
        body = self._body()
        self._record(body)
        path = self.path.rstrip("/")
        if path == "/login/api_key":
            token = jwt.encode(
                {"sub": "e2e-user", "exp": int(time.time()) + 3_600},
                "e2e-signing-key-with-at-least-32-bytes",
                algorithm="HS256",
            )
            self._respond_json({"access_token": token})
        elif path == "/otel/traces":
            action = self.server.trace_actions.pop(0) if self.server.trace_actions else {}
            if action.get("reset"):
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            delay = float(action.get("delay", 0))
            if delay:
                time.sleep(delay)
            self._respond_bytes(
                action.get("body", b""),
                status=int(action.get("status", 200)),
                headers=action.get("headers"),
            )
        elif path == f"/v2/projects/{_PROJECT_ID}/sessions/search":
            request = json.loads(body)
            external_id = next(
                (
                    item.get("value", "")
                    for item in request.get("filters", [])
                    if (item.get("column_id") or item.get("name")) == "external_id"
                ),
                "",
            )
            session_id = self.server.sessions_by_external_id.get(external_id)
            self._respond_json(
                {
                    "records": (
                        [{"id": session_id, "external_id": external_id}] if session_id else []
                    )
                }
            )
        elif path == f"/v2/projects/{_PROJECT_ID}/sessions":
            request = json.loads(body)
            external_id = request.get("external_id", "")
            self.server.sessions_by_external_id.setdefault(external_id, _SESSION_ID)
            self._respond_json(
                {
                    "id": self.server.sessions_by_external_id[external_id],
                    "name": request.get("name"),
                    "project_id": _PROJECT_ID,
                    "project_name": "hermes-native-e2e-project",
                    "previous_session_id": request.get("previous_session_id"),
                    "external_id": external_id,
                }
            )
        else:
            self._respond_json({"detail": "not found"}, status=404)


class _HermesContext:
    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.callbacks[name] = callback

    def emit(self, name: str, **payload: Any) -> None:
        self.callbacks[name](**payload, telemetry_schema_version="hermes.observer.v1")


def _any_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {item.key: _any_value(item.value) for item in value.kvlist_value.values}
    return getattr(value, kind)


def _attributes(items: Any) -> dict[str, Any]:
    return {item.key: _any_value(item.value) for item in items}


def _wire_spans(
    request: ExportTraceServiceRequest,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    resources: dict[str, Any] = {}
    spans: dict[str, dict[str, Any]] = {}
    for resource_spans in request.resource_spans:
        resources.update(_attributes(resource_spans.resource.attributes))
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                spans[span.name] = {
                    "trace_id": span.trace_id,
                    "span_id": span.span_id,
                    "parent_span_id": span.parent_span_id,
                    "attributes": _attributes(span.attributes),
                    "status": span.status.code,
                }
    return resources, spans


def _trace_calls(stub: _GalileoStub) -> list[dict[str, Any]]:
    return [call for call in stub.requests if call["path"].rstrip("/") == "/otel/traces"]


def _wait_for_trace_calls(
    stub: _GalileoStub,
    count: int,
    *,
    timeout: float = 3,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    calls: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        calls = _trace_calls(stub)
        if len(calls) >= count:
            return calls
        time.sleep(0.01)
    return calls


@pytest.mark.e2e
@pytest.mark.parametrize("capture_history", [False, True])
def test_complete_plugin_pipeline_exports_valid_otlp(
    monkeypatch: Any,
    tmp_path: Any,
    capture_history: bool,
) -> None:
    stub = _GalileoStub()
    server_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{stub.server_port}"

    for name, value in {
        "GALILEO_API_KEY": "galileo-e2e-api-key",
        "GALILEO_PROJECT": "hermes-e2e-project",
        "GALILEO_LOG_STREAM": "hermes-e2e-stream",
        "GALILEO_CONSOLE_URL": endpoint,
        "GALILEO_API_URL": endpoint,
        "GALILEO_HOME_DIR": str(tmp_path / "galileo"),
        "HERMES_GALILEO_CAPTURE_CONTENT": "true",
        "HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY": str(capture_history).lower(),
        "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "false",
        "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)

    GalileoPythonConfig._instance = None
    hooks.set_runtime(None)
    context = _HermesContext()

    try:
        hermes_galileo.register(context)
        runtime = hooks.get_runtime()
        assert runtime is not None

        base = {
            "session_id": "e2e-session",
            "turn_id": "e2e-turn",
            "task_id": "e2e-task",
            "model": "gpt-e2e",
            "provider": "openai",
            "platform": "cli",
        }
        context.emit(
            "on_session_start",
            **base,
            sender_id="person@example.test",
        )
        context.emit(
            "pre_llm_call",
            **base,
            user_message=(
                "Find deployment status\n"
                "Cookie: session_id=browser-cookie-secret; csrftoken=csrf-secret"
            ),
        )
        context.emit(
            "pre_api_request",
            **base,
            api_request_id="api-e2e",
            api_call_count=1,
            user_message="Find deployment status",
            request={
                "method": "POST",
                "body": {
                    "messages": [
                        {"role": "user", "content": "Find deployment status"},
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "Previous public answer"},
                                {
                                    "type": "thinking",
                                    "thinking": "wire-anthropic-thinking-secret",
                                    "signature": "wire-anthropic-signature-secret",
                                },
                                {
                                    "type": "redacted_thinking",
                                    "data": "wire-encrypted-reasoning-secret",
                                },
                            ],
                            "reasoning_content": "wire-hidden-reasoning-secret",
                            "extra_content": {
                                "google": {
                                    "thought_signature": "wire-gemini-signature-secret",
                                }
                            },
                        },
                    ],
                    "authorization": "Bearer provider-secret-token",
                },
            },
        )
        context.emit(
            "post_api_request",
            **base,
            api_request_id="api-e2e",
            api_call_count=1,
            response={
                "model": "gpt-e2e-version",
                "finish_reason": "stop",
                "assistant_message": {
                    "role": "assistant",
                    "content": "Deployment is healthy",
                },
                "usage": {
                    "input_tokens": 8,
                    "prompt_tokens": 13,
                    "cache_read_tokens": 5,
                    "output_tokens": 4,
                    "total_tokens": 17,
                },
            },
            response_model="gpt-e2e-version",
            finish_reason="stop",
            usage={
                "input_tokens": 8,
                "prompt_tokens": 13,
                "cache_read_tokens": 5,
                "output_tokens": 4,
                "total_tokens": 17,
            },
        )
        context.emit(
            "pre_tool_call",
            **base,
            tool_call_id="tool-e2e",
            api_request_id="api-e2e",
            tool_name="deployment_status",
            args={"authorization": "Bearer tool-secret-token"},
        )
        context.emit(
            "post_tool_call",
            **base,
            tool_call_id="tool-e2e",
            api_request_id="api-e2e",
            tool_name="deployment_status",
            result={
                "status": "healthy",
                "diagnostic": (
                    "-----BEGIN PRIVATE KEY-----\n"
                    "wire-private-key-material\n"
                    "-----END PRIVATE KEY-----"
                ),
                "image": "screenshot=data:image/png;base64,wire-base64-secret-0123456789",
            },
            status="ok",
        )
        context.emit(
            "post_llm_call",
            **base,
            assistant_response="Deployment is healthy",
        )
        context.emit("on_session_end", **base, completed=True)

        assert runtime._processor.wait_until_ready(5)
        assert hermes_galileo.force_flush() is True

        trace_calls = _wait_for_trace_calls(stub, 1)
        assert len(trace_calls) == 1

        call = trace_calls[0]
        assert call["headers"]["galileo-api-key"] == "galileo-e2e-api-key"
        assert call["headers"]["project"] == "hermes-e2e-project"
        assert call["headers"]["logstream"] == "hermes-e2e-stream"
        assert call["headers"]["content-type"] == "application/x-protobuf"

        export = ExportTraceServiceRequest.FromString(call["body"])
        resources, spans = _wire_spans(export)
        assert resources["service.name"] == "hermes-agent"
        assert resources["galileo.project.name"] == "hermes-e2e-project"
        assert resources["galileo.logstream.name"] == "hermes-e2e-stream"
        assert set(spans) == {
            "invoke_agent Hermes Agent",
            "chat gpt-e2e",
            "execute_tool deployment_status",
        }

        root = spans["invoke_agent Hermes Agent"]
        llm = spans["chat gpt-e2e"]
        tool = spans["execute_tool deployment_status"]
        assert root["parent_span_id"] == b""
        assert llm["parent_span_id"] == root["span_id"]
        assert tool["parent_span_id"] == root["span_id"]
        assert {span["trace_id"] for span in spans.values()} == {root["trace_id"]}
        assert llm["attributes"]["gen_ai.usage.input_tokens"] == 13
        assert llm["attributes"]["gen_ai.usage.output_tokens"] == 4
        assert json.loads(llm["attributes"]["gen_ai.output.messages"]) == [
            {"content": "Deployment is healthy", "role": "assistant"}
        ]

        wire_body = call["body"]
        assert b"provider-secret-token" not in wire_body
        assert b"tool-secret-token" not in wire_body
        assert b"browser-cookie-secret" not in wire_body
        assert b"csrf-secret" not in wire_body
        assert b"wire-private-key-material" not in wire_body
        assert b"wire-base64-secret" not in wire_body
        assert b"wire-hidden-reasoning-secret" not in wire_body
        assert b"wire-anthropic-thinking-secret" not in wire_body
        assert b"wire-anthropic-signature-secret" not in wire_body
        assert b"wire-encrypted-reasoning-secret" not in wire_body
        assert b"wire-gemini-signature-secret" not in wire_body
        assert b"person@example.test" not in wire_body
        assert b"Find deployment status" in wire_body
        if capture_history:
            assert b"Previous public answer" in wire_body
        else:
            assert b"Previous public answer" not in wire_body

        paths = [(item["method"], item["path"].rstrip("/")) for item in stub.requests]
        assert ("GET", "/healthcheck") in paths
        assert ("POST", "/login/api_key") in paths
        assert ("GET", "/current_user") in paths
    finally:
        runtime = hooks.get_runtime()
        if runtime is not None:
            runtime.shutdown()
        hooks.set_runtime(None)
        GalileoPythonConfig._instance = None
        stub.shutdown()
        stub.server_close()
        server_thread.join(timeout=1)


@pytest.mark.e2e
def test_official_sdk_native_session_is_shared_by_two_turn_traces(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    stub = _GalileoStub()
    server_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{stub.server_port}"
    raw_session_id = "native-e2e-raw-session"
    pseudonym_secret = "native-e2e-pseudonym-key"
    digest = hmac.new(
        pseudonym_secret.encode(),
        raw_session_id.encode(),
        hashlib.sha256,
    ).hexdigest()
    external_id = f"hermes:{digest}"

    for name, value in {
        "GALILEO_API_KEY": "galileo-native-e2e-api-key",
        "GALILEO_PROJECT": "hermes-native-e2e-project",
        "GALILEO_LOG_STREAM": "hermes-native-e2e-stream",
        "GALILEO_CONSOLE_URL": endpoint,
        "GALILEO_API_URL": endpoint,
        "GALILEO_HOME_DIR": str(tmp_path / "galileo"),
        "HERMES_GALILEO_CAPTURE_CONTENT": "false",
        "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "false",
        "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED": "true",
        "HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS": "2000",
        "HERMES_GALILEO_PSEUDONYM_SECRET": pseudonym_secret,
    }.items():
        monkeypatch.setenv(name, value)

    GalileoPythonConfig._instance = None
    hooks.set_runtime(None)
    context = _HermesContext()

    try:
        hermes_galileo.register(context)
        runtime = hooks.get_runtime()
        assert runtime is not None
        context.emit("on_session_start", session_id=raw_session_id)

        for turn_id in ("native-turn-one", "native-turn-two"):
            payload = {
                "session_id": raw_session_id,
                "turn_id": turn_id,
                "task_id": f"task-{turn_id}",
                "model": "gpt-native-e2e",
                "provider": "openai",
                "platform": "cli",
            }
            context.emit("pre_llm_call", **payload, user_message=f"input {turn_id}")
            context.emit(
                "post_llm_call",
                **payload,
                assistant_response=f"output {turn_id}",
            )
            context.emit("on_session_end", **payload, completed=True)

        assert runtime._processor.wait_until_ready(5)
        assert hermes_galileo.force_flush() is True
        snapshot = runtime.health_snapshot()
        assert snapshot["native_session_ready"] == 1
        assert snapshot["native_session_deferred_spans"] == 0

        trace_calls = _wait_for_trace_calls(stub, 1)
        assert trace_calls
        roots: list[tuple[dict[str, Any], dict[str, Any], bytes]] = []
        for call in trace_calls:
            export = ExportTraceServiceRequest.FromString(call["body"])
            for resource_spans in export.resource_spans:
                resource_attributes = _attributes(resource_spans.resource.attributes)
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        if span.name == "invoke_agent Hermes Agent":
                            roots.append(
                                (
                                    resource_attributes,
                                    _attributes(span.attributes),
                                    span.trace_id,
                                )
                            )

        assert len(roots) == 2
        assert len({trace_id for _, _, trace_id in roots}) == 2
        for resource_attributes, span_attributes, _ in roots:
            session_id = span_attributes.get(
                "galileo.session.id",
                resource_attributes.get("galileo.session.id"),
            )
            assert session_id == _SESSION_ID
            assert span_attributes["gen_ai.conversation.id"] == external_id
            assert span_attributes["hermes.session.id"] == external_id

        session_search_calls = [
            call
            for call in stub.requests
            if call["path"].split("?", 1)[0] == f"/v2/projects/{_PROJECT_ID}/sessions/search"
        ]
        session_create_calls = [
            call
            for call in stub.requests
            if call["path"].split("?", 1)[0] == f"/v2/projects/{_PROJECT_ID}/sessions"
        ]
        assert len(session_search_calls) == 1
        assert len(session_create_calls) == 1
        create_request = json.loads(session_create_calls[0]["body"])
        assert create_request["external_id"] == external_id
        assert create_request["log_stream_id"] == _LOG_STREAM_ID
        assert raw_session_id.encode() not in b"".join(call["body"] for call in stub.requests)
    finally:
        runtime = hooks.get_runtime()
        if runtime is not None:
            runtime.shutdown()
        hooks.set_runtime(None)
        GalileoPythonConfig._instance = None
        stub.shutdown()
        stub.server_close()
        server_thread.join(timeout=1)


_PARTIAL_SUCCESS = ExportTraceServiceResponse()
_PARTIAL_SUCCESS.partial_success.rejected_spans = 1
_PARTIAL_SUCCESS.partial_success.error_message = "synthetic rejection"


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("case", "actions", "timeout_millis", "expected_requests"),
    [
        ("unauthorized", [{"status": 401}, {"status": 200}], 2_000, 1),
        ("rate_limited", [{"status": 429}, {"status": 200}], 2_000, 1),
        (
            "rate_limited_retry_after",
            [
                {"status": 429, "headers": {"Retry-After": "0"}},
                {"status": 200},
            ],
            2_000,
            1,
        ),
        ("server_error", [{"status": 503}, {"status": 200}], 2_000, 2),
        ("request_timeout", [{"status": 408}, {"status": 200}], 2_000, 2),
        ("connection_reset", [{"reset": True}, {"status": 200}], 2_000, 2),
        ("read_timeout", [{"delay": 0.25, "status": 200}], 100, 1),
        (
            "partial_success",
            [{"status": 200, "body": _PARTIAL_SUCCESS.SerializeToString()}],
            2_000,
            1,
        ),
    ],
)
def test_official_sdk_otlp_failure_contract(
    monkeypatch: Any,
    tmp_path: Any,
    case: str,
    actions: list[dict[str, Any]],
    timeout_millis: int,
    expected_requests: int,
) -> None:
    """Pin the current official exporter behavior, including known gaps."""

    stub = _GalileoStub(actions)
    server_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{stub.server_port}"
    monkeypatch.setattr(otlp_http_exporter.random, "uniform", lambda _low, _high: 0.0)

    for name, value in {
        "GALILEO_API_KEY": "galileo-failure-matrix-key",
        "GALILEO_PROJECT": "hermes-failure-project",
        "GALILEO_LOG_STREAM": "hermes-failure-stream",
        "GALILEO_CONSOLE_URL": endpoint,
        "GALILEO_API_URL": endpoint,
        "GALILEO_HOME_DIR": str(tmp_path / "galileo"),
        "HERMES_GALILEO_CAPTURE_CONTENT": "true",
        "HERMES_GALILEO_MAX_CONTENT_CHARS": "256",
        "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "false",
        "HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS": str(timeout_millis),
        "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)

    GalileoPythonConfig._instance = None
    hooks.set_runtime(None)
    context = _HermesContext()

    try:
        hermes_galileo.register(context)
        runtime = hooks.get_runtime()
        assert runtime is not None
        assert runtime._processor.wait_until_ready(3)

        base = {
            "session_id": f"failure-{case}",
            "turn_id": f"turn-{case}",
            "task_id": f"task-{case}",
            "model": "gpt-failure-fixture",
            "provider": "openai",
            "platform": "cli",
        }
        context.emit(
            "pre_llm_call",
            **base,
            user_message="large-" + ("x" * 20_000),
        )
        context.emit("on_session_end", **base, completed=True)
        hermes_galileo.force_flush()

        trace_calls = _wait_for_trace_calls(stub, expected_requests)
        assert len(trace_calls) == expected_requests
        # Give non-retryable cases a chance to prove no second request occurs.
        time.sleep(0.05)
        assert len(_trace_calls(stub)) == expected_requests

        requests = [ExportTraceServiceRequest.FromString(call["body"]) for call in trace_calls]
        first_span = requests[0].resource_spans[0].scope_spans[0].spans[0]
        assert first_span.trace_id
        assert first_span.span_id
        assert b"x" * 1_000 not in trace_calls[0]["body"]
        for retried in requests[1:]:
            retried_span = retried.resource_spans[0].scope_spans[0].spans[0]
            assert retried_span.trace_id == first_span.trace_id
            assert retried_span.span_id == first_span.span_id
            assert retried.SerializeToString() == requests[0].SerializeToString()

        # GalileoSpanProcessor/OTel currently exposes neither an HTTP failure
        # count nor OTLP partialSuccess.rejectedSpans through our health API.
        assert runtime.health_snapshot()["dropped_spans"] == 0
    finally:
        runtime = hooks.get_runtime()
        if runtime is not None:
            runtime.shutdown()
        hooks.set_runtime(None)
        GalileoPythonConfig._instance = None
        stub.shutdown()
        stub.server_close()
        server_thread.join(timeout=1)
