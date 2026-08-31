"""ntfy notifications for newly opened Minerva seats."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlsplit

import aiohttp

from .registration import WaitlistOutcome, WaitlistResult

if TYPE_CHECKING:
    from .courses import AvailabilityOpening

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_REGISTRATION_URL = (
    "https://horizon.mcgill.ca/pban1/twbkwbis.P_GenMenu?name=bmenu.P_RegMnu"
)
logger = logging.getLogger(__name__)


class NotificationError(Exception):
    """A notification could not be delivered."""


class OpeningNotifier(Protocol):
    def notify_opening(self, opening: AvailabilityOpening) -> None: ...

    def notify_registration(
        self, opening: AvailabilityOpening, result: WaitlistResult
    ) -> None: ...


def _registration_notification(
    opening: AvailabilityOpening, result: WaitlistResult
) -> tuple[str, str]:
    section = opening.section
    if result.outcome is WaitlistOutcome.REGISTERED:
        title = f"Registered: {section.subject} {section.course}"
        action = "Registered directly"
    elif result.outcome is WaitlistOutcome.ALREADY_ADDED:
        title = f"Already waitlisted: {section.subject} {section.course}"
        action = "Already on the waitlist"
    else:
        title = f"Added to waitlist: {section.subject} {section.course}"
        action = "Successfully added to the waitlist"
    body = (
        f"{section.subject} {section.course}-{section.section}, CRN {section.crn}. "
        f"{action}."
    )
    return title, body


class DbusNotifier:
    """Send desktop notifications over D-Bus using notify-send."""

    def __init__(self, *, timeout: float = 10) -> None:
        self.timeout = timeout

    def notify_opening(self, opening: AvailabilityOpening) -> None:
        section = opening.section
        waitlist = opening.pool == "waitlist"
        title = (
            f"Waitlist spot opened: {section.subject} {section.course}"
            if waitlist
            else f"Seat opened: {section.subject} {section.course}"
        )
        pool = "Waitlist" if waitlist else "Seat"
        body = (
            f"{section.subject} {section.course}-{section.section}, CRN {section.crn}\n"
            f"{pool} availability: {opening.previous_remaining} → "
            f"{opening.current_remaining}"
        )
        logger.info(
            "sending D-Bus notification for %s %s-%s CRN %s",
            section.subject,
            section.course,
            section.section,
            section.crn,
        )
        self._send(title, body, urgency="low")

    def notify_registration(
        self, opening: AvailabilityOpening, result: WaitlistResult
    ) -> None:
        title, body = _registration_notification(opening, result)
        logger.info("sending D-Bus registration-success notification")
        self._send(title, body, urgency="low")

    def send_test(self) -> None:
        logger.info("sending test D-Bus notification")
        self._send(
            "seat-notifier test",
            "Desktop notifications are configured successfully.",
            urgency="low",
        )

    def _send(self, title: str, body: str, *, urgency: str) -> None:
        command = [
            "notify-send",
            "--app-name=seat-notifier",
            f"--urgency={urgency}",
            "--icon=dialog-information",
            title,
            body,
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as error:
            raise NotificationError("notify-send is not installed") from error
        except subprocess.TimeoutExpired as error:
            raise NotificationError("D-Bus notification timed out") from error
        except subprocess.CalledProcessError as error:
            raise NotificationError(
                "notify-send could not reach the desktop notification service"
            ) from error
        logger.info("D-Bus notification delivered")


class NtfyNotifier:
    def __init__(
        self,
        topic: str,
        *,
        server: str = DEFAULT_NTFY_SERVER,
        token: str | None = None,
        click_url: str = DEFAULT_REGISTRATION_URL,
        timeout: float = 15,
    ) -> None:
        if not topic.strip():
            raise ValueError("ntfy topic cannot be empty")
        parsed_server = urlsplit(server)
        if parsed_server.scheme not in {"http", "https"} or not parsed_server.netloc:
            raise ValueError("ntfy server must be an HTTP or HTTPS URL")
        parsed_click = urlsplit(click_url)
        if (
            parsed_click.scheme != "https"
            or parsed_click.hostname != "horizon.mcgill.ca"
        ):
            raise ValueError(
                "notification click URL must use HTTPS on horizon.mcgill.ca"
            )

        self.url = f"{server.rstrip('/')}/{quote(topic.strip(), safe='')}"
        self.token = token
        self.click_url = click_url
        self.timeout = timeout

    def notify_opening(self, opening: AvailabilityOpening) -> None:
        section = opening.section
        waitlist = opening.pool == "waitlist"
        title = (
            f"Waitlist spot opened: {section.subject} {section.course}"
            if waitlist
            else f"Seat opened: {section.subject} {section.course}"
        )
        pool = "Waitlist" if waitlist else "Seat"
        message = (
            f"{section.subject} {section.course}-{section.section}, CRN {section.crn}. "
            f"{pool} availability increased from {opening.previous_remaining} "
            f"to {opening.current_remaining}."
        )
        logger.info(
            "sending ntfy notification for %s %s-%s CRN %s",
            section.subject,
            section.course,
            section.section,
            section.crn,
        )
        self._send(
            title,
            message,
            tags="school,rotating_light",
            priority="low",
        )

    def notify_registration(
        self, opening: AvailabilityOpening, result: WaitlistResult
    ) -> None:
        title, message = _registration_notification(opening, result)
        logger.info("sending ntfy registration-success notification")
        self._send(
            title,
            message,
            tags="school,white_check_mark",
            priority="low",
        )

    def send_test(self) -> None:
        """Send a test alert using the same topic and click destination."""
        logger.info("sending test ntfy notification")
        self._send(
            "seat-notifier test",
            "Test notification delivered successfully. Click to open Minerva.",
            tags="school,white_check_mark",
            priority="low",
        )

    def _send(
        self,
        title: str,
        message: str,
        *,
        tags: str,
        priority: str,
    ) -> None:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": self.click_url,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            asyncio.run(self._post(message.encode("utf-8"), headers))
        except NotificationError:
            raise
        except TimeoutError as error:
            raise NotificationError("ntfy request timed out") from error
        except aiohttp.ClientError as error:
            raise NotificationError("could not connect to the ntfy server") from error
        logger.info("ntfy notification delivered")

    async def _post(self, body: bytes, headers: dict[str, str]) -> None:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(self.url, data=body, headers=headers) as response,
        ):
            await response.read()
            if response.status >= 400:
                raise NotificationError(f"ntfy returned HTTP {response.status}")
