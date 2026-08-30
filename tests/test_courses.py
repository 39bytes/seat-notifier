from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from email.message import Message
from io import StringIO
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qsl

from seat_notifier.client import MinervaResponse
from seat_notifier.courses import (
    CourseQuery,
    availability_openings,
    format_section,
    lecture_sections,
    parse_course_sections,
    poll_course,
    poll_courses,
    search_course,
)
from seat_notifier.notifications import NotificationError
from seat_notifier.registration import WaitlistOutcome, WaitlistResult

SECTIONS_HTML = """
<html><body>
<table class="datadisplaytable"><caption>Sections Found</caption>
<tr>
  <th>Select</th><th>CRN</th><th>Subj</th><th>Crse</th><th>Sec</th>
  <th>Type</th><th>Credits/CE Units</th><th>Title</th><th>Days</th><th>Time</th>
  <th>Cap</th><th>Act</th><th>Rem</th><th>WL Cap</th><th>WL Act</th><th>WL Rem</th>
  <th>Instructor</th><th>Date (MM/DD)</th><th>Location</th><th>Status</th>
</tr>
<tr>
  <td><input type="checkbox" name="sel_crn" value="2329 202609"></td>
  <td><a href="?crn_in=2329">2329</a></td><td>COMP</td><td>350</td><td>001</td>
  <td>Lecture</td><td>3.000</td><td>Numerical Computing.</td><td>WF</td>
  <td>10:05 am-11:25 am</td><td>180</td><td>178</td><td>2</td>
  <td>36</td><td>36</td><td>0</td><td>Example Instructor</td>
  <td>08/31-12/04</td><td>ROOM 26</td><td>Active</td>
</tr>
<tr><td>&nbsp;</td><td colspan="19">NOTES: Example note.</td></tr>
<tr>
  <td>C</td><td><a href="?crn_in=8557">8557</a></td><td>COMP</td><td>350</td><td>002</td>
  <td>Midterm Exam</td><td>0.000</td><td>Numerical Computing.</td><td>M</td>
  <td>06:35 pm-08:25 pm</td><td>0</td><td>0</td><td>0</td>
  <td>0</td><td>0</td><td>0</td><td>TBA</td>
  <td>10/19-10/21</td><td>ROOM 204</td><td>Registration Not Required</td>
</tr>
<tr>
  <td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td>
  <td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>M</td><td>06:35 pm-08:25 pm</td>
  <td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td>
  <td>TBA</td><td>10/19-10/21</td><td>ROOM 151</td><td>Registration Not Required</td>
</tr>
</table>
</body></html>
"""


class FakeClient:
    def __init__(self, html: str):
        self.html = html
        self.calls: list[dict[str, Any]] = []

    def request(self, path: str, **kwargs: Any) -> MinervaResponse:
        self.calls.append({"path": path, **kwargs})
        headers = Message()
        headers["Content-Type"] = "text/html; charset=UTF-8"
        return MinervaResponse(
            200, "https://horizon.mcgill.ca" + path, headers, self.html.encode()
        )


class FlakyNotifier:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def notify_opening(self, opening: object) -> None:
        self.calls.append(opening)
        if len(self.calls) == 1:
            raise NotificationError("temporary failure")

    def notify_registration(self, opening: object, result: object) -> None:
        pass


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.registrations: list[tuple[object, object]] = []

    def notify_opening(self, opening: object) -> None:
        self.calls.append(opening)

    def notify_registration(self, opening: object, result: object) -> None:
        self.registrations.append((opening, result))


class FullWaitlistRegistrar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def add_to_waitlist(self, term: str, crn: str) -> WaitlistResult:
        self.calls.append((term, crn))
        return WaitlistResult(
            crn, WaitlistOutcome.WAITLIST_FULL, "Open - Waitlist Full"
        )


class SuccessfulWaitlistRegistrar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def add_to_waitlist(self, term: str, crn: str) -> WaitlistResult:
        self.calls.append((term, crn))
        return WaitlistResult(crn, WaitlistOutcome.ADDED, "Added to waitlist")


class CourseTests(unittest.TestCase):
    def test_parses_sections_and_ignores_notes_and_continuation_rows(self) -> None:
        sections = parse_course_sections(SECTIONS_HTML)

        self.assertEqual(len(sections), 2)
        lecture, exam = sections
        self.assertEqual(lecture.crn, "2329")
        self.assertEqual(lecture.section, "001")
        self.assertEqual(lecture.capacity, 180)
        self.assertEqual(lecture.enrolled, 178)
        self.assertEqual(lecture.remaining, 2)
        self.assertEqual(lecture.waitlist_remaining, 0)
        self.assertEqual(lecture.instructor, "Example Instructor")
        self.assertEqual(lecture.dates, "08/31-12/04")
        self.assertTrue(lecture.selectable)
        self.assertTrue(lecture.has_waitlist_queue)
        self.assertFalse(lecture.available)
        self.assertFalse(exam.selectable)
        self.assertFalse(exam.available)

    def test_formats_availability(self) -> None:
        lecture = parse_course_sections(SECTIONS_HTML)[0]
        rendered = format_section(lecture)
        self.assertIn("[WAITLIST FULL] COMP 350-001 (CRN 2329)", rendered)
        self.assertIn("2 remaining / 180 capacity", rendered)
        self.assertIn("waitlist 0 remaining / 36 capacity", rendered)

    def test_filters_for_lecture_sections(self) -> None:
        sections = parse_course_sections(SECTIONS_HTML)
        filtered = lecture_sections(sections)
        self.assertEqual([section.crn for section in filtered], ["2329"])

    def test_reports_only_an_increase_in_waitlist_remaining(self) -> None:
        baseline = parse_course_sections(SECTIONS_HTML)[0]
        metadata_change = replace(baseline, location="ANOTHER ROOM")
        waitlist_decrease = replace(baseline, waitlist_remaining=0)
        waitlist_opening = replace(baseline, waitlist_enrolled=35, waitlist_remaining=1)

        self.assertEqual(availability_openings([baseline], [metadata_change]), ())
        self.assertEqual(availability_openings([baseline], [waitlist_decrease]), ())
        openings = availability_openings([baseline], [waitlist_opening])
        self.assertEqual(len(openings), 1)
        self.assertEqual(openings[0].pool, "waitlist")
        self.assertEqual(openings[0].opened, 1)

    def test_uses_regular_remaining_when_there_is_no_waitlist_queue(self) -> None:
        baseline = replace(
            parse_course_sections(SECTIONS_HTML)[0],
            waitlist_enrolled=0,
            waitlist_remaining=36,
            remaining=0,
        )
        seat_opening = replace(baseline, remaining=2)

        openings = availability_openings([baseline], [seat_opening])

        self.assertEqual(len(openings), 1)
        self.assertEqual(openings[0].pool, "section")
        self.assertEqual(openings[0].opened, 2)

    def test_checks_every_configured_course_in_one_cycle(self) -> None:
        queries = [
            CourseQuery("202609", "COMP", "350"),
            CourseQuery("202609", "MATH", "240"),
        ]
        sections = parse_course_sections(SECTIONS_HTML)

        with (
            patch(
                "seat_notifier.courses.search_course",
                side_effect=[sections, sections],
            ) as mocked_search,
            redirect_stdout(StringIO()),
        ):
            poll_courses(cast(Any, object()), queries, once=True)

        self.assertEqual(
            [call.args[1] for call in mocked_search.call_args_list], queries
        )

    def test_initial_open_waitlist_triggers_automatic_add(self) -> None:
        sections = parse_course_sections(SECTIONS_HTML)
        sections[0] = replace(sections[0], waitlist_enrolled=35, waitlist_remaining=1)
        registrar = FullWaitlistRegistrar()

        with (
            patch("seat_notifier.courses.search_course", return_value=sections),
            redirect_stdout(StringIO()),
            self.assertLogs("seat_notifier.courses", level="WARNING"),
        ):
            poll_courses(
                cast(Any, object()),
                [CourseQuery("202609", "COMP", "350")],
                once=True,
                waitlist_registrar=cast(Any, registrar),
            )

        self.assertEqual(registrar.calls, [("202609", "2329")])

    def test_successful_initial_waitlist_add_notifies_and_exits(self) -> None:
        sections = parse_course_sections(SECTIONS_HTML)
        sections[0] = replace(sections[0], waitlist_enrolled=35, waitlist_remaining=1)
        registrar = SuccessfulWaitlistRegistrar()
        notifier = RecordingNotifier()

        with (
            patch(
                "seat_notifier.courses.search_course", return_value=sections
            ) as search,
            patch("seat_notifier.courses.time.sleep") as sleep,
            redirect_stdout(StringIO()),
        ):
            poll_courses(
                cast(Any, object()),
                [CourseQuery("202609", "COMP", "350")],
                notifiers=[notifier],
                waitlist_registrar=cast(Any, registrar),
            )

        self.assertEqual(search.call_count, 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(registrar.calls, [("202609", "2329")])
        self.assertEqual(len(notifier.registrations), 1)
        opening, result = notifier.registrations[0]
        self.assertEqual(cast(Any, opening).section.crn, "2329")
        self.assertEqual(cast(Any, result).outcome, WaitlistOutcome.ADDED)

    def test_successful_course_is_removed_while_other_courses_continue(self) -> None:
        baseline = parse_course_sections(SECTIONS_HTML)
        open_waitlist = [
            replace(baseline[0], waitlist_enrolled=35, waitlist_remaining=1),
            baseline[1],
        ]
        comp = CourseQuery("202609", "COMP", "350")
        math = CourseQuery("202609", "MATH", "240")
        registrar = SuccessfulWaitlistRegistrar()

        with (
            patch(
                "seat_notifier.courses.search_course",
                side_effect=[open_waitlist, baseline, baseline],
            ) as search,
            patch(
                "seat_notifier.courses.time.sleep",
                side_effect=[None, KeyboardInterrupt],
            ),
            redirect_stdout(StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            poll_courses(
                cast(Any, object()),
                [comp, math],
                waitlist_registrar=cast(Any, registrar),
            )

        self.assertEqual(
            [call.args[1] for call in search.call_args_list],
            [comp, math, math],
        )

    def test_retries_a_failed_notification_on_the_next_poll(self) -> None:
        baseline = parse_course_sections(SECTIONS_HTML)
        opening = replace(baseline[0], waitlist_enrolled=35, waitlist_remaining=1)
        changed = [opening, baseline[1]]
        notifier = FlakyNotifier()
        successful_notifier = RecordingNotifier()
        registrar = FullWaitlistRegistrar()

        with (
            patch(
                "seat_notifier.courses.search_course",
                side_effect=[baseline, changed, changed],
            ),
            patch(
                "seat_notifier.courses.time.sleep",
                side_effect=[None, None, KeyboardInterrupt],
            ),
            redirect_stdout(StringIO()),
            self.assertLogs("seat_notifier.courses", level="WARNING"),
            self.assertRaises(KeyboardInterrupt),
        ):
            poll_course(
                cast(Any, object()),
                CourseQuery("202609", "COMP", "350"),
                interval=1,
                notifiers=[notifier, successful_notifier],
                waitlist_registrar=cast(Any, registrar),
            )

        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(len(successful_notifier.calls), 1)
        self.assertEqual(registrar.calls, [("202609", "2329")])

    def test_search_posts_captured_banner_form_with_duplicate_fields(self) -> None:
        client = FakeClient(SECTIONS_HTML)
        query = CourseQuery("202609", "comp", "350")

        sections = search_course(cast(Any, client), query)

        self.assertEqual(len(sections), 2)
        call = client.calls[0]
        self.assertEqual(call["path"], "/pban1/bwskfcls.P_GetCrse")
        self.assertEqual(call["method"], "POST")
        fields = parse_qsl(call["data"].decode(), keep_blank_values=True)
        self.assertEqual(
            [value for name, value in fields if name == "sel_subj"],
            ["dummy", "COMP"],
        )
        self.assertEqual(
            [value for name, value in fields if name == "SEL_INSTR"],
            ["dummy", "%"],
        )
        self.assertIn(("term_in", "202609"), fields)
        self.assertIn(("SEL_CRSE", "350"), fields)

    def test_rejects_invalid_query(self) -> None:
        with self.assertRaisesRegex(ValueError, "six-digit"):
            CourseQuery("fall-2026", "COMP", "350")


if __name__ == "__main__":
    unittest.main()
