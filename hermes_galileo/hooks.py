"""Fail-open Hermes hook callbacks.

Callbacks intentionally accept only ``**kwargs``. Hermes' observer contract is
additive, and accepting the entire payload keeps this plugin compatible with
new fields without changing Agent behavior.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .runtime import TelemetryRuntime

logger = logging.getLogger("hermes_galileo")

_RUNTIME: TelemetryRuntime | None = None
_RUNTIME_LOCK = threading.RLock()


def set_runtime(runtime: TelemetryRuntime | None) -> None:
    """Set the process runtime.

    Public primarily so embedders and tests can supply an isolated provider.
    """

    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


def get_runtime() -> TelemetryRuntime | None:
    with _RUNTIME_LOCK:
        return _RUNTIME


def _dispatch(method_name: str, payload: dict[str, Any]) -> None:
    runtime = get_runtime()
    if runtime is None:
        return
    try:
        getattr(runtime, method_name)(payload)
    except Exception:
        # Observer failures must never alter the user-visible agent path.
        # Deliberately do not log the payload: it can contain prompts/secrets.
        logger.warning("hermes-galileo hook %s failed", method_name, exc_info=True)


def on_session_start(**kwargs: Any) -> None:
    _dispatch("on_session_start", kwargs)


def on_session_end(**kwargs: Any) -> None:
    _dispatch("on_session_end", kwargs)


def on_session_finalize(**kwargs: Any) -> None:
    _dispatch("on_session_finalize", kwargs)


def on_session_reset(**kwargs: Any) -> None:
    _dispatch("on_session_reset", kwargs)


def on_pre_llm_call(**kwargs: Any) -> None:
    _dispatch("on_pre_llm_call", kwargs)


def on_post_llm_call(**kwargs: Any) -> None:
    _dispatch("on_post_llm_call", kwargs)


def on_pre_api_request(**kwargs: Any) -> None:
    _dispatch("on_pre_api_request", kwargs)


def on_post_api_request(**kwargs: Any) -> None:
    _dispatch("on_post_api_request", kwargs)


def on_api_request_error(**kwargs: Any) -> None:
    _dispatch("on_api_request_error", kwargs)


def on_pre_tool_call(**kwargs: Any) -> None:
    _dispatch("on_pre_tool_call", kwargs)


def on_post_tool_call(**kwargs: Any) -> None:
    _dispatch("on_post_tool_call", kwargs)


def on_pre_approval_request(**kwargs: Any) -> None:
    _dispatch("on_pre_approval_request", kwargs)


def on_post_approval_response(**kwargs: Any) -> None:
    _dispatch("on_post_approval_response", kwargs)


def on_subagent_start(**kwargs: Any) -> None:
    _dispatch("on_subagent_start", kwargs)


def on_subagent_stop(**kwargs: Any) -> None:
    _dispatch("on_subagent_stop", kwargs)
