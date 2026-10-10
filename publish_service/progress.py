from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PublishProgressReporter:
    """Persist the latest publish progress snapshot into publish_jobs.result_json.

    While a job is PENDING/RUNNING, result_json contains:

        {"_progress": { ... unified progress event ... }}

    A lightweight heartbeat updates the same snapshot while a worker is RUNNING.
    This heartbeat proves that the Publish Service process/job monitor is alive;
    it does not claim that a long-running child process is making forward progress.
    """

    store: Any
    job_id: str
    backend: str
    heartbeat_interval_seconds: float = 10.0
    _seq: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _heartbeat_stop: Event = field(default_factory=Event, init=False, repr=False)
    _heartbeat_thread: Thread | None = field(default=None, init=False, repr=False)
    _current_event: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def emit(
        self,
        stage: str,
        message: str,
        *,
        status: str = "RUNNING",
        progress: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = _utc_now()
            self._seq += 1
            event = {
                "seq": self._seq,
                "eventType": "progress",
                "jobId": str(self.job_id),
                "backend": self.backend,
                "status": status,
                "stage": stage,
                "message": message,
                "progress": progress,
                "detail": detail or {},
                "timestamp": now,
                "stageStartedAt": now,
                "heartbeatAt": now,
            }
            self.store.update_job_progress(self.job_id, event)
            self._current_event = event
            return dict(event)

    def start_heartbeat(self) -> None:
        with self._lock:
            if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
                return
            self._heartbeat_stop.clear()
            thread = Thread(
                target=self._heartbeat_loop,
                name=f"publish-heartbeat-{self.job_id}",
                daemon=True,
            )
            self._heartbeat_thread = thread
            thread.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self.heartbeat_interval_seconds + 1.0))
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        interval = max(2.0, float(self.heartbeat_interval_seconds))
        while not self._heartbeat_stop.wait(interval):
            try:
                self._touch_heartbeat()
            except Exception:
                logger.exception(
                    "Failed to persist publish heartbeat job_id=%s",
                    self.job_id,
                )

    def _touch_heartbeat(self) -> None:
        with self._lock:
            if self._current_event is None:
                return
            self._seq += 1
            heartbeat = dict(self._current_event)
            heartbeat.update(
                {
                    "seq": self._seq,
                    "eventType": "heartbeat",
                    "heartbeatAt": _utc_now(),
                }
            )
            self.store.update_job_progress(self.job_id, heartbeat)
            self._current_event = heartbeat
