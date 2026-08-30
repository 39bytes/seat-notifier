from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from seat_notifier.courses import AvailabilityOpening, parse_course_sections
from seat_notifier.notifications import (
    DEFAULT_REGISTRATION_URL,
    DbusNotifier,
    NtfyNotifier,
)
from seat_notifier.registration import WaitlistOutcome, WaitlistResult
from tests.test_courses import SECTIONS_HTML


class DbusNotifierTests(unittest.TestCase):
    def test_sends_course_and_crn_through_notify_send(self) -> None:
        section = parse_course_sections(SECTIONS_HTML)[0]
        opening = AvailabilityOpening(section, "waitlist", 0, 1)
        notifier = DbusNotifier()

        with patch("seat_notifier.notifications.subprocess.run") as mocked_run:
            notifier.notify_opening(opening)

        command = mocked_run.call_args.args[0]
        self.assertEqual(command[0], "notify-send")
        self.assertIn("--urgency=critical", command)
        self.assertIn("Waitlist spot opened: COMP 350", command)
        self.assertIn("COMP 350-001, CRN 2329", command[-1])
        self.assertIn("0 → 1", command[-1])
        self.assertTrue(mocked_run.call_args.kwargs["check"])

    def test_sends_registration_success_desktop_notification(self) -> None:
        section = parse_course_sections(SECTIONS_HTML)[0]
        opening = AvailabilityOpening(section, "waitlist", 0, 1)
        result = WaitlistResult("2329", WaitlistOutcome.ADDED, "added")
        notifier = DbusNotifier()

        with patch("seat_notifier.notifications.subprocess.run") as mocked_run:
            notifier.notify_registration(opening, result)

        command = mocked_run.call_args.args[0]
        self.assertIn("Added to waitlist: COMP 350", command)
        self.assertIn("CRN 2329", command[-1])

    def test_sends_test_desktop_notification(self) -> None:
        notifier = DbusNotifier()
        with patch("seat_notifier.notifications.subprocess.run") as mocked_run:
            notifier.send_test()
        self.assertIn("seat-notifier test", mocked_run.call_args.args[0])


class NtfyNotifierTests(unittest.TestCase):
    def test_sends_course_crn_and_minerva_click_target(self) -> None:
        section = parse_course_sections(SECTIONS_HTML)[0]
        opening = AvailabilityOpening(section, "waitlist", 0, 1)
        notifier = NtfyNotifier("private topic/test", token="secret-token")

        with patch.object(notifier, "_post", new_callable=AsyncMock) as mocked_post:
            notifier.notify_opening(opening)

        self.assertEqual(notifier.url, "https://ntfy.sh/private%20topic%2Ftest")
        awaited = mocked_post.await_args
        self.assertIsNotNone(awaited)
        assert awaited is not None
        body, headers = awaited.args
        self.assertEqual(headers["Title"], "Waitlist spot opened: COMP 350")
        self.assertEqual(headers["Click"], DEFAULT_REGISTRATION_URL)
        self.assertEqual(headers["Priority"], "high")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        message = body.decode()
        self.assertIn("COMP 350-001, CRN 2329", message)
        self.assertIn("increased from 0 to 1", message)

    def test_sends_registration_success_ntfy_notification(self) -> None:
        section = parse_course_sections(SECTIONS_HTML)[0]
        opening = AvailabilityOpening(section, "waitlist", 0, 1)
        result = WaitlistResult("2329", WaitlistOutcome.REGISTERED, "registered")
        notifier = NtfyNotifier("test-topic")

        with patch.object(notifier, "_post", new_callable=AsyncMock) as mocked_post:
            notifier.notify_registration(opening, result)

        awaited = mocked_post.await_args
        self.assertIsNotNone(awaited)
        assert awaited is not None
        body, headers = awaited.args
        self.assertEqual(headers["Title"], "Registered: COMP 350")
        self.assertIn("CRN 2329", body.decode())

    def test_sends_test_notification(self) -> None:
        notifier = NtfyNotifier("test-topic")

        with patch.object(notifier, "_post", new_callable=AsyncMock) as mocked_post:
            notifier.send_test()

        self.assertEqual(notifier.url, "https://ntfy.sh/test-topic")
        awaited = mocked_post.await_args
        self.assertIsNotNone(awaited)
        assert awaited is not None
        body, headers = awaited.args
        self.assertEqual(headers["Title"], "seat-notifier test")
        self.assertEqual(headers["Click"], DEFAULT_REGISTRATION_URL)
        self.assertIn("Test notification delivered successfully", body.decode())

    def test_rejects_non_minerva_click_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "horizon.mcgill.ca"):
            NtfyNotifier("topic", click_url="https://example.com/collect")


if __name__ == "__main__":
    unittest.main()
