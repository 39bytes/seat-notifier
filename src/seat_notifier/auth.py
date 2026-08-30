"""Firefox/Geckodriver authentication for McGill Minerva."""

from __future__ import annotations

import json
import logging
import os
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlsplit

import pyotp
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from .client import cookies_from_webdriver

LOGIN_URL = "https://horizon.mcgill.ca/ssomanager/saml/login?relayState=/c/auth/SSB"

logger = logging.getLogger(__name__)

_USERNAME = ((By.ID, "i0116"), (By.NAME, "loginfmt"))
_PASSWORD = ((By.ID, "i0118"), (By.NAME, "passwd"))
_TOTP = (
    (By.ID, "idTxtBx_SAOTCC_OTC"),
    (By.NAME, "otc"),
    (By.CSS_SELECTOR, 'input[autocomplete="one-time-code"]'),
)
_OTHER_METHOD = (
    (By.ID, "signInAnotherWay"),
    (By.CSS_SELECTOR, '[data-bind*="showMethods"]'),
)
_TOTP_METHOD = (
    (By.CSS_SELECTOR, '[data-value="PhoneAppOTP"]'),
    (By.ID, "idA_SAASTO_TOTP"),
    (
        By.XPATH,
        (
            "//*[self::div or self::a or self::button][contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verification code')]"
        ),
    ),
)
_SUBMIT = ((By.ID, "idSIButton9"), (By.CSS_SELECTOR, 'input[type="submit"]'))
_TOTP_SUBMIT = (
    (By.ID, "idSubmit_SAOTCC_Continue"),
    (By.ID, "idSIButton9"),
    (By.CSS_SELECTOR, 'input[type="submit"]'),
)
_LOGIN_ERRORS = (
    (By.ID, "usernameError"),
    (By.ID, "passwordError"),
    (By.ID, "idDiv_SAOTCC_Error_OTC"),
)


class BrowserAuthenticationError(Exception):
    """The browser could not establish an authenticated Minerva session."""


def _first_visible(
    driver: WebDriver, selectors: tuple[tuple[str, str], ...]
) -> WebElement | None:
    for by, value in selectors:
        for element in driver.find_elements(by, value):
            if element.is_displayed() and element.is_enabled():
                return element
    return None


def _wait_visible(
    driver: WebDriver, selectors: tuple[tuple[str, str], ...], timeout: float
) -> WebElement:
    try:
        return WebDriverWait(driver, timeout).until(
            lambda current: _first_visible(current, selectors) or False
        )
    except TimeoutException as error:
        raise BrowserAuthenticationError(
            "the expected Microsoft sign-in control did not appear"
        ) from error


def _has_minerva_session(driver: WebDriver) -> bool:
    return urlsplit(driver.current_url).hostname == "horizon.mcgill.ca" and any(
        cookie.get("name") == "SESSID" for cookie in driver.get_cookies()
    )


def _has_login_error(driver: WebDriver) -> bool:
    return _first_visible(driver, _LOGIN_ERRORS) is not None


def _safe_page_description(driver: WebDriver) -> str:
    """Describe a failed page without including its potentially sensitive query string."""
    hostname = urlsplit(driver.current_url).hostname or "unknown host"
    return f"{driver.title!r} on {hostname}"


def save_webdriver_cookies(cookies: list[dict[str, object]], path: str | Path) -> None:
    """Atomically save WebDriver cookies in a mode-0600 JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(cookies, output)
            output.write("\n")
        os.replace(temporary, destination)
        destination.chmod(0o600)
        logger.info("saved refreshed cookies to %s", destination)
    finally:
        temporary.unlink(missing_ok=True)


class GeckoAuthenticator:
    """Authenticate through Microsoft SSO in an isolated Firefox session."""

    def __init__(
        self,
        username: str,
        password: str,
        totp_secret: str,
        *,
        headless: bool = True,
        timeout: float = 120,
        geckodriver_path: str | None = None,
        cookie_cache: str | Path | None = None,
    ) -> None:
        if not username or not password or not totp_secret:
            raise ValueError("username, password, and TOTP secret are required")
        self.username = username
        self.password = password
        self.totp = pyotp.TOTP(totp_secret.replace(" ", ""))
        # Validate the Base32 secret now rather than halfway through login.
        try:
            self.totp.now()
        except Exception as error:
            raise ValueError(
                "MINERVA_TOTP_SECRET is not a valid Base32 TOTP secret"
            ) from error
        self.headless = headless
        self.timeout = timeout
        self.geckodriver_path = geckodriver_path
        self.cookie_cache = Path(cookie_cache) if cookie_cache else None

    @classmethod
    def from_environment(
        cls,
        *,
        headless: bool = True,
        timeout: float = 120,
        cookie_cache: str | Path | None = None,
    ) -> GeckoAuthenticator:
        names = ("MINERVA_USERNAME", "MINERVA_PASSWORD", "MINERVA_TOTP_SECRET")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ValueError(
                f"missing authentication environment variable(s): {', '.join(missing)}"
            )
        return cls(
            os.environ["MINERVA_USERNAME"],
            os.environ["MINERVA_PASSWORD"],
            os.environ["MINERVA_TOTP_SECRET"],
            headless=headless,
            timeout=timeout,
            geckodriver_path=os.environ.get("GECKODRIVER_PATH"),
            cookie_cache=cookie_cache,
        )

    def authenticate(self) -> CookieJar:
        logger.info(
            "launching Firefox through Geckodriver (%s)",
            "headless" if self.headless else "headed",
        )
        options = webdriver.FirefoxOptions()
        if self.headless:
            options.add_argument("-headless")
        # Geckodriver and Firefox do not need to inherit account secrets.
        service_environment = {
            name: value
            for name, value in os.environ.items()
            if name
            not in {"MINERVA_USERNAME", "MINERVA_PASSWORD", "MINERVA_TOTP_SECRET"}
        }
        service = Service(
            executable_path=self.geckodriver_path,
            env=service_environment,
        )
        try:
            driver = webdriver.Firefox(options=options, service=service)
        except WebDriverException as error:
            raise BrowserAuthenticationError(
                "could not start Firefox through Geckodriver"
            ) from error

        try:
            try:
                self._complete_login(driver)
                browser_cookies = driver.get_cookies()
            except BrowserAuthenticationError:
                raise
            except WebDriverException as error:
                raise BrowserAuthenticationError(
                    "Firefox failed while completing Microsoft sign-in"
                ) from error

            logger.info(
                "Firefox reached Minerva and returned %d cookies", len(browser_cookies)
            )
            if not any(cookie.get("name") == "SESSID" for cookie in browser_cookies):
                raise BrowserAuthenticationError(
                    "login completed without a Minerva SESSID cookie"
                )
            if self.cookie_cache:
                save_webdriver_cookies(browser_cookies, self.cookie_cache)
            return cookies_from_webdriver(browser_cookies)
        finally:
            try:
                logger.debug("closing Firefox")
                driver.quit()
            except WebDriverException:
                pass

    def _complete_login(self, driver: WebDriver) -> None:
        logger.info("opening the McGill SSO login flow")
        driver.get(LOGIN_URL)

        username = _wait_visible(driver, _USERNAME, self.timeout)
        logger.info("submitting the Microsoft account username")
        username.clear()
        username.send_keys(self.username)
        _wait_visible(driver, _SUBMIT, 10).click()

        password = _wait_visible(driver, _PASSWORD, self.timeout)
        logger.info("submitting the Microsoft account password")
        password.clear()
        password.send_keys(self.password)
        _wait_visible(driver, _SUBMIT, 10).click()

        self._complete_mfa_or_redirect(driver)

    def _complete_mfa_or_redirect(self, driver: WebDriver) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if _has_minerva_session(driver):
                logger.info("Minerva session established without a TOTP prompt")
                return
            if _has_login_error(driver):
                raise BrowserAuthenticationError("Microsoft rejected a sign-in value")

            totp_input = _first_visible(driver, _TOTP)
            if totp_input:
                logger.info("Microsoft requested a verification code")
                self._submit_totp(driver, totp_input)
                self._finish_redirect(driver, deadline)
                return

            other_method = _first_visible(driver, _OTHER_METHOD)
            if other_method:
                logger.info("selecting verification-code authentication")
                other_method.click()
                totp_method = _wait_visible(
                    driver, _TOTP_METHOD, max(1, deadline - time.monotonic())
                )
                totp_method.click()
                totp_input = _wait_visible(
                    driver, _TOTP, max(1, deadline - time.monotonic())
                )
                self._submit_totp(driver, totp_input)
                self._finish_redirect(driver, deadline)
                return

            # A trusted Microsoft session can skip MFA and show "Stay signed in?".
            submit = _first_visible(driver, _SUBMIT)
            if submit and "stay signed in" in driver.page_source.lower():
                submit.click()
            time.sleep(0.25)

        raise BrowserAuthenticationError(
            f"timed out waiting for MFA or Minerva redirect at {_safe_page_description(driver)}"
        )

    def _submit_totp(self, driver: WebDriver, field: WebElement) -> None:
        seconds_remaining = self.totp.interval - (time.time() % self.totp.interval)
        if seconds_remaining < 5:
            logger.debug("waiting for the next TOTP time window")
            time.sleep(seconds_remaining + 0.25)
        logger.info("submitting a generated TOTP code")
        field.clear()
        field.send_keys(self.totp.now())
        _wait_visible(driver, _TOTP_SUBMIT, 10).click()

    def _finish_redirect(self, driver: WebDriver, deadline: float) -> None:
        while time.monotonic() < deadline:
            if _has_minerva_session(driver):
                logger.info("Minerva session established after TOTP verification")
                return
            if _has_login_error(driver):
                raise BrowserAuthenticationError("Microsoft rejected the TOTP code")
            submit = _first_visible(driver, _SUBMIT)
            if submit and "stay signed in" in driver.page_source.lower():
                submit.click()
            time.sleep(0.25)
        raise BrowserAuthenticationError(
            f"timed out waiting for Minerva after MFA at {_safe_page_description(driver)}"
        )
