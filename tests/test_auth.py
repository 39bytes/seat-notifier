from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from seat_notifier.auth import GeckoAuthenticator, save_webdriver_cookies


class GeckoAuthenticatorTests(unittest.TestCase):
    def test_rejects_invalid_totp_secret_before_opening_browser(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid Base32"):
            GeckoAuthenticator("user", "password", "not a base32 secret!")

    def test_cookie_cache_is_private_json(self) -> None:
        cookies: list[dict[str, object]] = [
            {
                "name": "SESSID",
                "value": "secret",
                "domain": "horizon.mcgill.ca",
                "path": "/",
                "secure": True,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "cookies.json")
            save_webdriver_cookies(cookies, destination)

            self.assertEqual(json.loads(destination.read_text()), cookies)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
