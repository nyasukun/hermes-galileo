"""Hermes Agent → Galileo observability plugin."""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any

from . import hooks
from .config import ConfigurationError, Settings
from .runtime import RuntimeInitializationError, TelemetryRuntime

logger = logging.getLogger("hermes_galileo")

_INIT_LOCK = threading.RLock()
_ATEXIT_REGISTERED = False

_HOOKS = (
    ("on_session_start", hooks.on_session_start),
    ("on_session_end", hooks.on_session_end),
    ("on_session_finalize", hooks.on_session_finalize),
    ("on_session_reset", hooks.on_session_reset),
    ("pre_llm_call", hooks.on_pre_llm_call),
    ("post_llm_call", hooks.on_post_llm_call),
    ("pre_api_request", hooks.on_pre_api_request),
    ("post_api_request", hooks.on_post_api_request),
    ("api_request_error", hooks.on_api_request_error),
    ("pre_tool_call", hooks.on_pre_tool_call),
    ("post_tool_call", hooks.on_post_tool_call),
    ("pre_approval_request", hooks.on_pre_approval_request),
    ("post_approval_response", hooks.on_post_approval_response),
    ("subagent_start", hooks.on_subagent_start),
    ("subagent_stop", hooks.on_subagent_stop),
)


def _valid_hermes_hooks() -> set[str] | None:
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return set(VALID_HOOKS)
    except Exception:
        return None


def _configure_logging(debug: bool) -> None:
    if debug:
        logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    # Avoid duplicate output when Hermes has already configured root logging.
    logger.propagate = False


def _shutdown_runtime() -> None:
    runtime = hooks.get_runtime()
    if runtime is not None:
        # Stop new dispatches before draining the runtime. A callback that
        # already obtained the reference is still rejected by the runtime's
        # lock-protected shutdown gate.
        hooks.set_runtime(None)
        runtime.shutdown()


def initialize(settings: Settings | None = None) -> TelemetryRuntime | None:
    """Initialize once and return the active runtime.

    Missing credentials, disabled logging, or SDK initialization errors disable
    only observability. The Hermes Agent continues normally.
    """

    global _ATEXIT_REGISTERED
    with _INIT_LOCK:
        existing = hooks.get_runtime()
        if existing is not None:
            return existing

        try:
            resolved = settings or Settings.from_env()
        except ConfigurationError as exc:
            _configure_logging(False)
            logger.warning("hermes-galileo disabled: invalid configuration: %s", exc)
            return None

        _configure_logging(resolved.debug)
        if not resolved.enabled:
            logger.info("hermes-galileo disabled by configuration")
            return None

        missing = resolved.missing_required()
        if missing:
            logger.warning(
                "hermes-galileo disabled: set %s in the active Hermes .env",
                ", ".join(missing),
            )
            return None

        # The Galileo SDK consumes these variables for authentication and
        # custom-deployment routing. Settings may be supplied programmatically
        # by an embedder, so ensure the SDK sees the same validated values.
        os.environ["GALILEO_API_KEY"] = resolved.api_key
        os.environ["GALILEO_PROJECT"] = resolved.project
        os.environ["GALILEO_LOG_STREAM"] = resolved.log_stream
        if resolved.console_url:
            os.environ["GALILEO_CONSOLE_URL"] = resolved.console_url
        else:
            os.environ.pop("GALILEO_CONSOLE_URL", None)
        if resolved.api_url:
            os.environ["GALILEO_API_URL"] = resolved.api_url
        else:
            os.environ.pop("GALILEO_API_URL", None)

        try:
            runtime = TelemetryRuntime(resolved)
        except RuntimeInitializationError as exc:
            logger.warning("hermes-galileo disabled: %s", exc)
            return None
        except Exception as exc:
            logger.warning("hermes-galileo disabled: SDK initialization failed: %s", exc)
            return None

        hooks.set_runtime(runtime)
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_runtime)
            _ATEXIT_REGISTERED = True
        logger.info(
            "hermes-galileo ready: project=%s log_stream=%s content_capture=%s",
            resolved.project,
            resolved.log_stream,
            "enabled" if resolved.capture_content else "disabled",
        )
        return runtime


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    SDK network initialization is deferred inside ``TelemetryRuntime``. This
    call creates local tracing state immediately and never waits for Galileo.
    """

    runtime = initialize()
    if runtime is None:
        return

    valid_hooks = _valid_hermes_hooks()
    registered = 0
    for name, callback in _HOOKS:
        if valid_hooks is not None and name not in valid_hooks:
            logger.debug("Hermes does not support hook %s; skipping", name)
            continue
        try:
            ctx.register_hook(name, callback)
            registered += 1
        except Exception:
            logger.debug("Could not register optional Hermes hook %s", name, exc_info=True)
    logger.info("hermes-galileo registered %d observer hooks", registered)


def force_flush() -> bool:
    """Flush pending spans without exposing the SDK object."""

    runtime = hooks.get_runtime()
    return True if runtime is None else runtime.force_flush()


def health_snapshot() -> dict[str, Any]:
    """Return a secret-free status snapshot for diagnostics."""

    runtime = hooks.get_runtime()
    if runtime is None:
        return {"enabled": False}
    return runtime.health_snapshot()


__all__ = [
    "force_flush",
    "health_snapshot",
    "initialize",
    "register",
]
