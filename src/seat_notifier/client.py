"""Small HTTP client for McGill's legacy Minerva/Banner application."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE_URL = "https://horizon.mcgill.ca/"
MENU_PATH = "/pban1/twbkwbis.P_GenMenu?name=bmenu.P_MainMnu"
DEFAULT_USER_AGENT = "seat-notifier/0.1"

logger = logging.getLogger(__name__)


class MinervaError(Exception):
    """Base error raised by the Minerva client."""


class AuthenticationRequired(MinervaError):
    """The server returned Minerva's login page."""


class Authenticator(Protocol):
    """A provider capable of obtaining a fresh Minerva cookie jar."""

    def authenticate(self) -> CookieJar: ...


@dataclass(frozen=True)
class MinervaResponse:
    status: int
    url: str
    headers: Message
    body: bytes

    @property
    def text(self) -> str:
        charset = self.headers.get_content_charset() or "utf-8"
        return self.body.decode(charset, errors="replace")

    @property
    def appears_logged_out(self) -> bool:
        page = self.text.lower()
        login_form = (
            "twbkwbis.p_vallogin" in page
            and 'name="sid"' in page
            and 'name="pin"' in page
        )
        # Depending on how the SSO session expires, Banner can return a tiny
        # HTTP-200 page that immediately sends the browser through logout.
        logout_refresh = "twbkwbis.p_idm_logout" in page
        return login_form or logout_refresh


def _cookie(
    name: str,
    value: str,
    *,
    domain: str = "horizon.mcgill.ca",
    path: str = "/",
    secure: bool = True,
    expires: int | None = None,
) -> Cookie:
    normalized_domain = domain.lstrip(".").lower()
    if normalized_domain not in {"mcgill.ca", "horizon.mcgill.ca"}:
        raise ValueError(f"refusing cookie for non-Minerva domain: {domain!r}")

    # WebDriver may omit the leading dot from a parent-domain cookie.
    parent_domain = normalized_domain == "mcgill.ca"
    cookie_domain = f".{normalized_domain}" if parent_domain else normalized_domain

    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=cookie_domain,
        domain_specified=True,
        domain_initial_dot=parent_domain,
        path=path or "/",
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""},
        rfc2109=False,
    )


def cookies_from_header(raw_header: str) -> CookieJar:
    """Create a cookie jar from a browser's raw Cookie request header."""
    parsed = SimpleCookie()
    parsed.load(raw_header)
    if not parsed:
        raise ValueError("cookie header did not contain any cookies")

    jar = CookieJar()
    for name, morsel in parsed.items():
        jar.set_cookie(_cookie(name, morsel.value))
    return jar


def cookies_from_webdriver(data: list[dict[str, Any]]) -> CookieJar:
    """Convert the list produced by Selenium's ``driver.get_cookies()``."""
    jar = CookieJar()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise TypeError("invalid WebDriver cookie entry")
        value = item.get("value")
        if not isinstance(value, str):
            raise TypeError(f"cookie {item['name']!r} has no string value")
        jar.set_cookie(
            _cookie(
                item["name"],
                value,
                domain=item.get("domain", "horizon.mcgill.ca"),
                path=item.get("path", "/"),
                secure=bool(item.get("secure", True)),
                expires=int(item["expiry"]) if item.get("expiry") is not None else None,
            )
        )
    return jar


def cookies_from_webdriver_json(path: str | Path) -> CookieJar:
    """Load the list produced by Selenium's ``driver.get_cookies()``."""
    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError("WebDriver cookie file must contain a JSON list")
    return cookies_from_webdriver(data)


class MinervaClient:
    """Stateful, same-origin Minerva client backed by a cookie jar."""

    def __init__(
        self,
        cookies: CookieJar | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        authenticator: Authenticator | None = None,
    ):
        self.cookies = cookies if cookies is not None else CookieJar()
        self.user_agent = user_agent
        self.authenticator = authenticator
        self._opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _replace_cookies(self, fresh: CookieJar) -> None:
        self.cookies.clear()
        for cookie in fresh:
            self.cookies.set_cookie(cookie)
        logger.info("installed %d refreshed Minerva cookies", len(self.cookies))

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        require_auth: bool = True,
        timeout: float = 30,
    ) -> MinervaResponse:
        url = urljoin(BASE_URL, path)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "horizon.mcgill.ca":
            raise ValueError("refusing to send Minerva cookies to another origin")

        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if headers:
            request_headers.update(headers)
        for attempt in range(2):
            logger.debug(
                "sending %s request to %s (attempt %d)",
                method,
                parsed.path,
                attempt + 1,
            )
            request = Request(url, data=data, headers=request_headers, method=method)
            try:
                with self._opener.open(request, timeout=timeout) as raw_response:
                    response = MinervaResponse(
                        status=raw_response.status,
                        url=raw_response.url,
                        headers=raw_response.headers,
                        body=raw_response.read(),
                    )
            except HTTPError as error:
                raise MinervaError(
                    f"Minerva returned HTTP {error.code} for {parsed.path}"
                ) from error
            except URLError as error:
                raise MinervaError(
                    f"request to Minerva failed for {parsed.path}"
                ) from error

            logger.debug(
                "received HTTP %d from %s (%d bytes)",
                response.status,
                parsed.path,
                len(response.body),
            )
            if not require_auth or not response.appears_logged_out:
                return response

            logger.info("Minerva reported an absent or expired session")
            if attempt == 0 and self.authenticator is not None:
                logger.info("starting automatic browser authentication")
                self._replace_cookies(self.authenticator.authenticate())
                logger.info("browser authentication succeeded; retrying request")
                continue
            raise AuthenticationRequired("Minerva session is absent or expired")

        raise AssertionError("unreachable")

    def probe(self) -> MinervaResponse:
        """Request the main menu and fail if it resolves to the login page."""
        return self.request(MENU_PATH)

    def cookie_names(self) -> list[str]:
        """Return cookie names only, suitable for diagnostics without leaking values."""
        return sorted(cookie.name for cookie in self.cookies)
