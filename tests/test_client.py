from __future__ import annotations

import unittest
from email.message import Message
from http.cookiejar import CookieJar
from typing import Any, Self, cast
from urllib.request import Request

from seat_notifier.client import (
    AuthenticationRequired,
    MinervaClient,
    cookies_from_header,
    cookies_from_webdriver,
)

LOGIN_PAGE = b"""<form action="/pban1/twbkwbis.P_ValLogin">
<input name="sid"><input name="PIN"></form>"""
LOGOUT_REFRESH = b"""<html><head><meta http-equiv="refresh"
content="0;url=/pban1/twbkwbis.p_idm_logout"></head></html>"""
AUTHENTICATED_PAGE = b"<html><title>Main Menu</title></html>"


class FakeRawResponse:
    def __init__(self, body: bytes):
        self.status = 200
        self.url = "https://horizon.mcgill.ca/pban1/test"
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=UTF-8"
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, *bodies: bytes):
        self.bodies = list(bodies)
        self.calls = 0

    def open(self, request: object, timeout: float) -> FakeRawResponse:
        self.calls += 1
        return FakeRawResponse(self.bodies.pop(0))


class FakeAuthenticator:
    def __init__(self):
        self.calls = 0

    def authenticate(self) -> CookieJar:
        self.calls += 1
        return cookies_from_header("SESSID=fresh; TESTID=set")


class MinervaClientTests(unittest.TestCase):
    def test_expired_session_authenticates_and_retries_once(self) -> None:
        authenticator = FakeAuthenticator()
        client = MinervaClient(
            cookies_from_header("SESSID=expired"), authenticator=authenticator
        )
        opener = FakeOpener(LOGIN_PAGE, AUTHENTICATED_PAGE)
        client._opener = cast(Any, opener)

        response = client.request("/pban1/test")

        self.assertEqual(response.body, AUTHENTICATED_PAGE)
        self.assertEqual(authenticator.calls, 1)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(client.cookie_names(), ["SESSID", "TESTID"])

    def test_logout_meta_refresh_authenticates_and_retries(self) -> None:
        authenticator = FakeAuthenticator()
        client = MinervaClient(authenticator=authenticator)
        opener = FakeOpener(LOGOUT_REFRESH, AUTHENTICATED_PAGE)
        client._opener = cast(Any, opener)

        response = client.request("/pban1/test")

        self.assertEqual(response.body, AUTHENTICATED_PAGE)
        self.assertEqual(authenticator.calls, 1)
        self.assertEqual(opener.calls, 2)

    def test_expired_session_without_authenticator_fails(self) -> None:
        client = MinervaClient(cookies_from_header("SESSID=expired"))
        opener = FakeOpener(LOGIN_PAGE)
        client._opener = cast(Any, opener)

        with self.assertRaises(AuthenticationRequired):
            client.request("/pban1/test")

        self.assertEqual(opener.calls, 1)

    def test_parent_mcgill_domain_cookie_is_sent_to_horizon(self) -> None:
        jar = cookies_from_webdriver(
            [
                {
                    "name": "parent-cookie",
                    "value": "value",
                    "domain": "mcgill.ca",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        request = Request("https://horizon.mcgill.ca/pban1/test")

        jar.add_cookie_header(request)

        self.assertEqual(request.get_header("Cookie"), "parent-cookie=value")

    def test_non_mcgill_cookie_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cookies_from_webdriver(
                [
                    {
                        "name": "unexpected",
                        "value": "secret",
                        "domain": "example.com",
                    }
                ]
            )

    def test_cookies_are_never_sent_to_another_origin(self) -> None:
        client = MinervaClient(cookies_from_header("SESSID=secret"))
        with self.assertRaises(ValueError):
            client.request("https://example.com/collect")


if __name__ == "__main__":
    unittest.main()
