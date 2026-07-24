"""Bounded, asynchronous Galileo native-session provisioning."""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .config import Settings

logger = logging.getLogger("hermes_galileo")

_SessionStatus = Literal["disabled", "pending", "ready", "failed"]


@dataclass(frozen=True, slots=True)
class NativeSessionResolution:
    """A secret-free snapshot of one local native-session mapping."""

    status: _SessionStatus
    galileo_session_id: str = ""
    generation: int = 0


@dataclass(slots=True)
class _SessionEntry:
    generation: int
    status: _SessionStatus
    deadline_monotonic: float
    last_used_monotonic: float
    galileo_session_id: str = ""
    release_after_resolution: bool = False


class NativeSessionManager:
    """Map pseudonymous Hermes session keys to Galileo Session UUIDs.

    All SDK construction and calls run on a fixed number of daemon workers.
    ``ensure`` and lifecycle methods perform bounded in-memory work only.
    """

    _WORKER_COUNT = 2
    _MAX_ENTRIES = 512
    _QUEUE_CAPACITY = 512

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], Any],
        on_resolved: Callable[[str, str | None, int], None],
        configuration_validator: Callable[[], None] | None = None,
        max_entries: int | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._on_resolved = on_resolved
        self._configuration_validator = configuration_validator
        self._max_entries = max_entries or self._MAX_ENTRIES
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._state_changed = threading.Event()
        self._state = "running"
        self._generation = 0
        self._callbacks_inflight = 0
        self._worker_calls_inflight = 0
        self._entries: dict[str, _SessionEntry] = {}
        self._jobs: queue.Queue[tuple[str, int]] = queue.Queue(
            maxsize=queue_capacity or self._QUEUE_CAPACITY
        )
        self._stats = {
            "native_session_attempts": 0,
            "native_session_resolved": 0,
            "native_session_failures": 0,
            "native_session_timeouts": 0,
            "native_session_capacity_rejections": 0,
            "native_session_mapping_evictions": 0,
            "native_session_cancelled": 0,
        }
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"hermes-galileo-session-{index + 1}",
                daemon=True,
            )
            for index in range(self._WORKER_COUNT)
        ]
        self._monitor = threading.Thread(
            target=self._monitor_deadlines,
            name="hermes-galileo-session-deadlines",
            daemon=True,
        )
        for worker in self._workers:
            worker.start()
        self._monitor.start()

    @staticmethod
    def _snapshot(entry: _SessionEntry) -> NativeSessionResolution:
        return NativeSessionResolution(
            status=entry.status,
            galileo_session_id=entry.galileo_session_id,
            generation=entry.generation,
        )

    def _evict_one_locked(self) -> bool:
        candidates = [
            (external_id, entry)
            for external_id, entry in self._entries.items()
            if entry.status == "failed"
        ]
        if not candidates:
            return False
        external_id, _ = min(
            candidates,
            key=lambda item: item[1].last_used_monotonic,
        )
        self._entries.pop(external_id, None)
        self._stats["native_session_mapping_evictions"] += 1
        return True

    def ensure(self, external_id: str) -> NativeSessionResolution:
        """Return or asynchronously begin one idempotent Session lookup."""

        if not external_id or self._stopping.is_set():
            return NativeSessionResolution("disabled")

        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(external_id)
            if entry is not None and entry.release_after_resolution:
                if entry.status == "failed":
                    # The old lifecycle ended while its failure callback
                    # was in flight. Reopening must use a new generation.
                    self._entries.pop(external_id, None)
                    entry = None
                else:
                    # Reopening before resolution cancels the requested
                    # local release without blocking the hook thread.
                    entry.release_after_resolution = False
                    entry.last_used_monotonic = now
                    self._state_changed.set()
                    return self._snapshot(entry)
            if entry is not None:
                entry.last_used_monotonic = now
                return self._snapshot(entry)
            if len(self._entries) >= self._max_entries and not self._evict_one_locked():
                self._stats["native_session_capacity_rejections"] += 1
                return NativeSessionResolution("failed")

            self._generation += 1
            generation = self._generation
            entry = _SessionEntry(
                generation=generation,
                status="pending",
                deadline_monotonic=(now + self._settings.native_session_timeout_millis / 1_000),
                last_used_monotonic=now,
            )
            self._entries[external_id] = entry
            try:
                self._jobs.put_nowait((external_id, generation))
            except queue.Full:
                self._entries.pop(external_id, None)
                self._stats["native_session_capacity_rejections"] += 1
                return NativeSessionResolution("failed")
            self._state_changed.set()
            return self._snapshot(entry)

    def lookup(self, external_id: str) -> NativeSessionResolution:
        if not external_id:
            return NativeSessionResolution("disabled")
        with self._lock:
            entry = self._entries.get(external_id)
            if entry is None:
                return NativeSessionResolution("disabled")
            entry.last_used_monotonic = time.monotonic()
            return self._snapshot(entry)

    def finalize(self, external_id: str) -> None:
        """Release only the local mapping; the remote Session is retained."""

        with self._lock:
            entry = self._entries.get(external_id)
            if entry is None:
                return
            if entry.status == "pending":
                if not entry.release_after_resolution:
                    entry.release_after_resolution = True
                return
            self._entries.pop(external_id, None)
            self._state_changed.set()

    def abandon(self, external_id: str) -> None:
        """Immediately discard an obsolete local lookup without remote deletion.

        This is used when a late subagent hook reveals that a child key must
        follow its top-level parent's mapping. A worker already inside the
        public SDK call may still create the deduplicated remote Session, but
        its late result is ignored locally.
        """

        with self._lock:
            self._entries.pop(external_id, None)
            self._state_changed.set()

    def _notify(self, external_id: str, session_id: str | None, generation: int) -> None:
        try:
            self._on_resolved(external_id, session_id, generation)
        except Exception:
            logger.warning("Native Galileo Session callback failed", exc_info=True)

    @staticmethod
    def _validated_session_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Galileo start_session returned no Session ID")
        return str(uuid.UUID(text))

    def _complete(
        self,
        external_id: str,
        generation: int,
        *,
        session_id: str | None,
    ) -> None:
        with self._lock:
            entry = self._entries.get(external_id)
            if entry is None or entry.generation != generation or entry.status != "pending":
                return
            if session_id:
                entry.status = "ready"
                entry.galileo_session_id = session_id
                self._stats["native_session_resolved"] += 1
            else:
                entry.status = "failed"
                self._stats["native_session_failures"] += 1
            entry.last_used_monotonic = time.monotonic()
            self._callbacks_inflight += 1
        self._notify(external_id, session_id, generation)
        with self._lock:
            self._callbacks_inflight -= 1
            entry = self._entries.get(external_id)
            if (
                entry is not None
                and entry.generation == generation
                and entry.release_after_resolution
            ):
                self._entries.pop(external_id, None)
            self._state_changed.set()

    def _worker_loop(self) -> None:
        client: Any | None = None
        try:
            while not self._stopping.is_set():
                try:
                    external_id, generation = self._jobs.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._stopping.is_set():
                    self._jobs.task_done()
                    break

                with self._lock:
                    entry = self._entries.get(external_id)
                    current = (
                        entry is not None
                        and entry.generation == generation
                        and entry.status == "pending"
                    )
                    if current:
                        self._stats["native_session_attempts"] += 1
                        self._worker_calls_inflight += 1
                if not current:
                    self._jobs.task_done()
                    continue

                try:
                    if self._configuration_validator is not None:
                        self._configuration_validator()
                    if client is None:
                        client = self._client_factory()
                    # Logger construction can initialize the process-global SDK
                    # singleton. Revalidate immediately before Session API use.
                    if self._configuration_validator is not None:
                        self._configuration_validator()
                    session_id = self._validated_session_id(
                        client.start_session(
                            name="Hermes Agent session",
                            external_id=external_id,
                            metadata={
                                "service.name": self._settings.service_name,
                                "deployment.environment.name": self._settings.environment,
                            },
                        )
                    )
                except Exception:
                    logger.warning(
                        "Galileo native Session resolution failed; spans will "
                        "continue without galileo.session.id",
                        exc_info=self._settings.debug,
                    )
                    self._complete(external_id, generation, session_id=None)
                else:
                    self._complete(external_id, generation, session_id=session_id)
                finally:
                    if client is not None:
                        try:
                            client.clear_session()
                        except Exception:
                            logger.debug(
                                "Could not clear GalileoLogger Session context",
                                exc_info=True,
                            )
                    with self._lock:
                        self._worker_calls_inflight -= 1
                        self._state_changed.set()
                    self._jobs.task_done()
        finally:
            if client is not None:
                try:
                    client.terminate()
                except Exception:
                    logger.debug("Could not terminate GalileoLogger", exc_info=True)

    def _monitor_deadlines(self) -> None:
        interval = min(
            max(self._settings.native_session_timeout_millis / 4_000, 0.05),
            0.25,
        )
        while not self._stopping.wait(interval):
            now = time.monotonic()
            expired: list[tuple[str, int]] = []
            with self._lock:
                for external_id, entry in self._entries.items():
                    if entry.status == "pending" and entry.deadline_monotonic <= now:
                        entry.status = "failed"
                        entry.last_used_monotonic = now
                        self._stats["native_session_timeouts"] += 1
                        self._callbacks_inflight += 1
                        expired.append((external_id, entry.generation))
            for external_id, generation in expired:
                self._notify(external_id, None, generation)
                with self._lock:
                    self._callbacks_inflight -= 1
                    entry = self._entries.get(external_id)
                    if (
                        entry is not None
                        and entry.generation == generation
                        and entry.release_after_resolution
                    ):
                        self._entries.pop(external_id, None)
                    self._state_changed.set()

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        """Wait only on an explicit flush/shutdown path, never a Hermes hook."""

        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            with self._lock:
                idle = (
                    not any(entry.status == "pending" for entry in self._entries.values())
                    and self._callbacks_inflight == 0
                )
                if idle:
                    return True
                self._state_changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._state_changed.wait(remaining)

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                status: sum(entry.status == status for entry in self._entries.values())
                for status in ("pending", "ready", "failed")
            }
            return {
                "native_sessions_enabled": True,
                "native_session_state": self._state,
                "native_session_pending": counts["pending"],
                "native_session_ready": counts["ready"],
                "native_session_failed": counts["failed"],
                "native_session_mappings": len(self._entries),
                "native_session_release_pending": sum(
                    entry.release_after_resolution for entry in self._entries.values()
                ),
                "native_session_queue_depth": self._jobs.qsize(),
                "native_session_callbacks_inflight": self._callbacks_inflight,
                "native_session_worker_calls_inflight": self._worker_calls_inflight,
                "native_session_worker_cleanup_deferred": (
                    self._state == "stopped" and any(worker.is_alive() for worker in self._workers)
                ),
                **self._stats,
            }

    def cancel_all(self) -> None:
        callbacks: list[tuple[str, int]] = []
        with self._lock:
            for external_id, entry in self._entries.items():
                if entry.status == "pending":
                    callbacks.append((external_id, entry.generation))
                    self._stats["native_session_cancelled"] += 1
            self._entries.clear()
            self._callbacks_inflight += len(callbacks)
        for external_id, generation in callbacks:
            self._notify(external_id, None, generation)
            with self._lock:
                self._callbacks_inflight -= 1
                self._state_changed.set()

    def cancel_pending(self) -> None:
        """Fail-open pending mappings while retaining resolved local mappings."""

        callbacks: list[tuple[str, int]] = []
        with self._lock:
            now = time.monotonic()
            for external_id, entry in self._entries.items():
                if entry.status != "pending":
                    continue
                entry.status = "failed"
                entry.last_used_monotonic = now
                callbacks.append((external_id, entry.generation))
                self._stats["native_session_cancelled"] += 1
            self._callbacks_inflight += len(callbacks)
        for external_id, generation in callbacks:
            self._notify(external_id, None, generation)
            with self._lock:
                self._callbacks_inflight -= 1
                entry = self._entries.get(external_id)
                if (
                    entry is not None
                    and entry.generation == generation
                    and entry.release_after_resolution
                ):
                    self._entries.pop(external_id, None)
                self._state_changed.set()

    def shutdown(self, timeout_seconds: float) -> None:
        with self._lock:
            if self._state in {"stopping", "stopped"}:
                return
            self._state = "stopping"
            self._stopping.set()
        self.cancel_all()
        deadline = time.monotonic() + max(timeout_seconds, 0)
        self._monitor.join(timeout=max(deadline - time.monotonic(), 0))
        for worker in self._workers:
            worker.join(timeout=max(deadline - time.monotonic(), 0))
        with self._lock:
            self._state = "stopped"
