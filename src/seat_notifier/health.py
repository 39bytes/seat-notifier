"""Small threaded health server for container deployments."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from .notifications import NotificationError

logger = logging.getLogger(__name__)


class TestNotifier(Protocol):
    def send_test(self) -> None: ...


@dataclass
class HealthState:
    notifiers: tuple[TestNotifier, ...]
    notification_sent: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def notify_once(self) -> str:
        if not self.notifiers:
            return "disabled"
        with self.lock:
            if self.notification_sent:
                return "already-sent"
            for notifier in self.notifiers:
                notifier.send_test()
            self.notification_sent = True
            return "sent"


class HealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: HealthState):
        self.state = state
        super().__init__(address, HealthHandler)


class HealthHandler(BaseHTTPRequestHandler):
    server: HealthServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"status": "not-found"})
            return
        try:
            notification = self.server.state.notify_once()
        except NotificationError as error:
            logger.warning("health-check test notification failed: %s", error)
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "unhealthy", "notification": "failed"},
            )
            return
        self._json(
            HTTPStatus.OK,
            {"status": "ok", "notification": notification},
        )

    def _json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("health request: " + format, *args)


def start_health_server(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    notifiers: tuple[TestNotifier, ...] = (),
) -> HealthServer:
    if not 0 <= port <= 65535:
        raise ValueError("health port must be between 0 and 65535")
    server = HealthServer((host, port), HealthState(notifiers))
    thread = threading.Thread(
        target=server.serve_forever,
        name="seat-notifier-health",
        daemon=True,
    )
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    logger.info("health server listening on %s:%s", bound_host, bound_port)
    return server
