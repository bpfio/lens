"""Structured JSON logger with optional file output and rotation.

Mirrors JS src/middleware/logger.js.
"""

import json
import os
import sys
import time
import queue
import threading

_LOG_FILE = None
_LOG_QUEUE: queue.Queue | None = None
_WORKER_THREAD: threading.Thread | None = None


def _log_worker():
    """Background worker for non-blocking disk I/O."""
    while True:
        try:
            entry = _LOG_QUEUE.get()
            if entry is None:
                break
            path = _LOG_FILE
            if path is not None:
                line = json.dumps(entry, default=str) + "\n"
                try:
                    if os.path.exists(path) and os.path.getsize(path) > 10 * 1024 * 1024:
                        for i in range(4, 0, -1):
                            old = f"{path}.{i}" if i > 0 else path
                            new = f"{path}.{i + 1}"
                            if os.path.exists(old):
                                os.rename(old, new)
                        if os.path.exists(path):
                            os.rename(path, f"{path}.1")
                except OSError:
                    pass
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass
        finally:
            _LOG_QUEUE.task_done()


def configure_log_file(log_dir: str = "/var/log/lens") -> str:
    """Set up file logging with simple rotation on SIGHUP.

    Called once at app startup. Idempotent.
    Returns the log file path.
    """
    global _LOG_FILE, _LOG_QUEUE, _WORKER_THREAD
    if _LOG_FILE is not None:
        return _LOG_FILE

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "lens.log")
    _LOG_FILE = log_path

    if _LOG_QUEUE is None:
        _LOG_QUEUE = queue.Queue(maxsize=10000)
        _WORKER_THREAD = threading.Thread(target=_log_worker, daemon=True, name="LensLogWorker")
        _WORKER_THREAD.start()

    return log_path


def _write_file(entry: dict) -> None:
    """Queue a log entry for non-blocking file write."""
    if _LOG_QUEUE is not None:
        try:
            _LOG_QUEUE.put_nowait(entry)
        except queue.Full:
            pass


class Logger:
    """Logger that emits structured JSON lines to stdout/stderr and optionally a file."""

    def __init__(self, request_id: str = "unknown"):
        self._request_id = request_id

    def info(self, event: str, data: dict | None = None) -> None:
        self._emit("info", event, data)

    def warn(self, event: str, data: dict | None = None) -> None:
        self._emit("warn", event, data)

    def error(self, event: str, data: dict | None = None) -> None:
        self._emit("error", event, data)

    def _emit(self, level: str, event: str, data: dict | None = None) -> None:
        entry = {
            "level": level,
            "event": event,
            "requestId": self._request_id,
            "timestamp": int(time.time() * 1000),
            **(data or {}),
        }
        line = json.dumps(entry, default=str)

        # stdout / stderr
        if level == "error":
            print(line, file=sys.stderr, flush=True)
        else:
            print(line, flush=True)

        # File output
        _write_file(entry)


def create_logger(request_id: str = "unknown") -> Logger:
    """Create a structured JSON logger instance."""
    return Logger(request_id)
