from __future__ import annotations

import json
import unittest
from urllib.request import urlopen

from seat_notifier.health import start_health_server


class FakeTestNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send_test(self) -> None:
        self.calls += 1


class HealthServerTests(unittest.TestCase):
    def test_health_sends_one_notification_per_process(self) -> None:
        notifier = FakeTestNotifier()
        server = start_health_server(host="127.0.0.1", port=0, notifiers=(notifier,))
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                first = json.loads(response.read())
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                second = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(first, {"status": "ok", "notification": "sent"})
        self.assertEqual(second, {"status": "ok", "notification": "already-sent"})
        self.assertEqual(notifier.calls, 1)

    def test_health_can_run_without_notification_channels(self) -> None:
        server = start_health_server(host="127.0.0.1", port=0)
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(payload, {"status": "ok", "notification": "disabled"})


if __name__ == "__main__":
    unittest.main()
