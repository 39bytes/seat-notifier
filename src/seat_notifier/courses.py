"""Course-section lookup and polling for Minerva's legacy Banner pages."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from .client import AuthenticationRequired, MinervaClient, MinervaError
from .notifications import NotificationError, OpeningNotifier
from .registration import (
    WaitlistError,
    WaitlistOutcome,
    WaitlistRegistrar,
    WaitlistResult,
)

SEARCH_PATH = "/pban1/bwskfcls.P_GetCrse"
logger = logging.getLogger(__name__)


class CourseSearchError(Exception):
    """Minerva returned a course-search page that could not be interpreted."""


@dataclass(frozen=True)
class CourseQuery:
    term: str
    subject: str
    course: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{6}", self.term):
            raise ValueError("term must be a six-digit Banner term such as 202609")
        if not re.fullmatch(r"[A-Za-z0-9]{2,8}", self.subject):
            raise ValueError("subject must contain 2-8 letters or digits")
        if not re.fullmatch(r"[A-Za-z0-9]{1,10}", self.course):
            raise ValueError("course must contain 1-10 letters or digits")
        object.__setattr__(self, "subject", self.subject.upper())
        object.__setattr__(self, "course", self.course.upper())


@dataclass(frozen=True)
class CourseSection:
    crn: str
    subject: str
    course: str
    section: str
    section_type: str
    title: str
    days: str
    meeting_time: str
    capacity: int | None
    enrolled: int | None
    remaining: int | None
    waitlist_capacity: int | None
    waitlist_enrolled: int | None
    waitlist_remaining: int | None
    instructor: str
    dates: str
    location: str
    status: str
    selectable: bool

    @property
    def has_waitlist_queue(self) -> bool:
        return self.waitlist_enrolled is not None and self.waitlist_enrolled > 0

    @property
    def available(self) -> bool:
        """Whether the currently relevant registration pool has an opening."""
        if self.has_waitlist_queue:
            return self.waitlist_available
        return self.selectable and self.remaining is not None and self.remaining > 0

    @property
    def waitlist_available(self) -> bool:
        return self.waitlist_remaining is not None and self.waitlist_remaining > 0


@dataclass(frozen=True)
class AvailabilityOpening:
    section: CourseSection
    pool: str
    previous_remaining: int
    current_remaining: int

    @property
    def opened(self) -> int:
        return self.current_remaining - self.previous_remaining


_HEADERS = {
    "crn": "crn",
    "subj": "subject",
    "crse": "course",
    "sec": "section",
    "type": "section_type",
    "title": "title",
    "days": "days",
    "time": "meeting_time",
    "cap": "capacity",
    "act": "enrolled",
    "rem": "remaining",
    "wl cap": "waitlist_capacity",
    "wl act": "waitlist_enrolled",
    "wl rem": "waitlist_remaining",
    "instructor": "instructor",
    "location": "location",
    "status": "status",
}
_INTEGER_FIELDS = {
    "capacity",
    "enrolled",
    "remaining",
    "waitlist_capacity",
    "waitlist_enrolled",
    "waitlist_remaining",
}
_NO_RESULTS_MARKERS = (
    "no classes were found that meet your search criteria",
    "no sections found",
)


def _text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _normalized_header(cell: Tag) -> str:
    return _text(cell).lower().rstrip(":")


def _integer(value: str) -> int | None:
    return int(value) if re.fullmatch(r"-?\d+", value) else None


def parse_course_sections(html: str) -> list[CourseSection]:
    """Parse section and seat counts from a Banner "Sections Found" table."""
    soup = BeautifulSoup(html, "html.parser")
    sections: list[CourseSection] = []
    found_header = False

    for table in soup.find_all("table"):
        columns: dict[str, int] | None = None
        for row in table.find_all("tr"):
            header_cells = row.find_all("th", recursive=False)
            if header_cells:
                labels = [_normalized_header(cell) for cell in header_cells]
                if {"crn", "subj", "crse", "cap", "rem"}.issubset(labels):
                    columns = {
                        canonical: index
                        for index, label in enumerate(labels)
                        if (canonical := _HEADERS.get(label)) is not None
                    }
                    # Date has nested abbreviation text and is easier to identify
                    # by its stable position between Instructor and Location.
                    for index, label in enumerate(labels):
                        if label.startswith("date"):
                            columns["dates"] = index
                    found_header = True
                continue

            if columns is None:
                continue
            cells = row.find_all("td", recursive=False)
            crn_index = columns["crn"]
            if crn_index >= len(cells):
                continue
            crn = _text(cells[crn_index])
            # Notes, spacer rows, and continuation meeting rows have no CRN.
            if not crn.isdigit():
                continue

            raw_values: dict[str, str] = {}
            for name in (*_HEADERS.values(), "dates"):
                index = columns.get(name)
                raw_values[name] = (
                    _text(cells[index])
                    if index is not None and index < len(cells)
                    else ""
                )

            select_cell = cells[0]
            selectable = (
                select_cell.find("input", attrs={"type": "checkbox"}) is not None
            )
            values: dict[str, str | int | None] = {
                name: _integer(raw_value) if name in _INTEGER_FIELDS else raw_value
                for name, raw_value in raw_values.items()
            }
            sections.append(
                CourseSection(
                    crn=crn,
                    subject=str(values["subject"]),
                    course=str(values["course"]),
                    section=str(values["section"]),
                    section_type=str(values["section_type"]),
                    title=str(values["title"]),
                    days=str(values["days"]),
                    meeting_time=str(values["meeting_time"]),
                    capacity=values["capacity"]
                    if isinstance(values["capacity"], int)
                    else None,
                    enrolled=values["enrolled"]
                    if isinstance(values["enrolled"], int)
                    else None,
                    remaining=values["remaining"]
                    if isinstance(values["remaining"], int)
                    else None,
                    waitlist_capacity=(
                        values["waitlist_capacity"]
                        if isinstance(values["waitlist_capacity"], int)
                        else None
                    ),
                    waitlist_enrolled=(
                        values["waitlist_enrolled"]
                        if isinstance(values["waitlist_enrolled"], int)
                        else None
                    ),
                    waitlist_remaining=(
                        values["waitlist_remaining"]
                        if isinstance(values["waitlist_remaining"], int)
                        else None
                    ),
                    instructor=str(values["instructor"]),
                    dates=str(values["dates"]),
                    location=str(values["location"]),
                    status=str(values["status"]),
                    selectable=selectable,
                )
            )

    if found_header:
        return sections
    page_text = " ".join(soup.get_text(" ", strip=True).lower().split())
    if any(marker in page_text for marker in _NO_RESULTS_MARKERS):
        return []
    raise CourseSearchError(
        "Minerva response did not contain a recognizable sections table"
    )


def _search_form(query: CourseQuery) -> list[tuple[str, str]]:
    """Return fields in Banner's expected order, preserving duplicate names."""
    return [
        ("term_in", query.term),
        ("sel_subj", "dummy"),
        ("sel_subj", query.subject),
        ("SEL_CRSE", query.course),
        ("SEL_TITLE", ""),
        ("BEGIN_HH", "0"),
        ("BEGIN_MI", "0"),
        ("BEGIN_AP", "a"),
        ("SEL_DAY", "dummy"),
        ("SEL_PTRM", "dummy"),
        ("END_HH", "0"),
        ("END_MI", "0"),
        ("END_AP", "a"),
        ("SEL_CAMP", "dummy"),
        ("SEL_SCHD", "dummy"),
        ("SEL_SESS", "dummy"),
        ("SEL_INSTR", "dummy"),
        ("SEL_INSTR", "%"),
        ("SEL_ATTR", "dummy"),
        ("SEL_ATTR", "%"),
        ("SEL_LEVL", "dummy"),
        ("SEL_LEVL", "%"),
        ("SEL_INSM", "dummy"),
        ("sel_dunt_code", ""),
        ("sel_dunt_unit", ""),
        ("call_value_in", ""),
        ("rsts", "dummy"),
        ("crn", "dummy"),
        ("path", "1"),
        ("SUB_BTN", "View Sections"),
    ]


def search_course(client: MinervaClient, query: CourseQuery) -> list[CourseSection]:
    logger.info("checking %s %s for term %s", query.subject, query.course, query.term)
    body = urlencode(_search_form(query)).encode("ascii")
    response = client.request(
        SEARCH_PATH,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://horizon.mcgill.ca",
            "Referer": "https://horizon.mcgill.ca/pban1/bwskfcls.P_GetCrse",
        },
    )
    sections = parse_course_sections(response.text)
    logger.info("found %d matching sections", len(sections))
    return sections


def lecture_sections(sections: Iterable[CourseSection]) -> tuple[CourseSection, ...]:
    """Keep only primary Lecture rows, excluding exams, labs, and tutorials."""
    return tuple(
        section for section in sections if section.section_type.casefold() == "lecture"
    )


def availability_openings(
    previous: Iterable[CourseSection], current: Iterable[CourseSection]
) -> tuple[AvailabilityOpening, ...]:
    """Return only increases in the currently relevant availability pool."""
    previous_by_crn = {section.crn: section for section in previous}
    openings: list[AvailabilityOpening] = []
    for section in current:
        old = previous_by_crn.get(section.crn)
        if old is None:
            continue

        if section.has_waitlist_queue:
            pool = "waitlist"
            before = old.waitlist_remaining
            after = section.waitlist_remaining
        else:
            pool = "section"
            before = old.remaining
            after = section.remaining

        if before is not None and after is not None and after > before:
            openings.append(AvailabilityOpening(section, pool, before, after))
    return tuple(openings)


def format_section(section: CourseSection) -> str:
    if section.has_waitlist_queue:
        state = "WAITLIST OPEN" if section.waitlist_available else "WAITLIST FULL"
    else:
        state = "OPEN" if section.available else "CLOSED"
    seats = f"{section.remaining if section.remaining is not None else '?'} remaining"
    if section.capacity is not None:
        seats += f" / {section.capacity} capacity"
    waitlist = ""
    if section.waitlist_capacity is not None:
        waitlist = (
            f"; waitlist {section.waitlist_remaining if section.waitlist_remaining is not None else '?'}"
            f" remaining / {section.waitlist_capacity} capacity"
        )
    meeting = " ".join(
        part for part in (section.days, section.meeting_time, section.location) if part
    )
    return (
        f"[{state}] {section.subject} {section.course}-{section.section} "
        f"(CRN {section.crn}): {seats}{waitlist} | {section.title} | {meeting}"
    ).rstrip(" |")


def print_sections(
    sections: Iterable[CourseSection], *, query: CourseQuery | None = None
) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    label = f" — {query.subject} {query.course}" if query else ""
    print(f"\n{timestamp}{label}")
    materialized = list(sections)
    if not materialized:
        print("No matching Lecture sections found.")
        return
    for section in materialized:
        print(format_section(section))


def print_openings(openings: Iterable[AvailabilityOpening]) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"\n{timestamp}")
    for opening in openings:
        label = "WAITLIST SPOT OPENED" if opening.pool == "waitlist" else "SEAT OPENED"
        print(f"{label} (+{opening.opened}): {format_section(opening.section)}")


def poll_courses(
    client: MinervaClient,
    queries: Iterable[CourseQuery],
    *,
    interval: float = 60,
    once: bool = False,
    notifiers: Iterable[OpeningNotifier] = (),
    waitlist_registrar: WaitlistRegistrar | None = None,
) -> None:
    if interval <= 0:
        raise ValueError("poll interval must be greater than zero")
    configured_queries = tuple(dict.fromkeys(queries))
    if not configured_queries:
        raise ValueError("at least one course is required")

    active_queries = list(configured_queries)
    previous: dict[CourseQuery, tuple[CourseSection, ...]] = {}
    configured_notifiers = tuple(notifiers)
    pending_notifications: dict[
        tuple[int, str, str, str], tuple[OpeningNotifier, AvailabilityOpening]
    ] = {}
    pending_registration_notifications: dict[
        tuple[int, str, str],
        tuple[OpeningNotifier, AvailabilityOpening, WaitlistResult],
    ] = {}
    pending_waitlist_adds: dict[tuple[CourseQuery, str], AvailabilityOpening] = {}
    check_number = 0
    while True:
        check_number += 1
        logger.info(
            "course availability cycle %d (%d courses)",
            check_number,
            len(active_queries),
        )
        for query in tuple(active_queries):
            try:
                sections = lecture_sections(search_course(client, query))
            except AuthenticationRequired:
                raise
            except MinervaError as error:
                if once:
                    raise
                logger.warning(
                    "%s %s availability check failed: %s",
                    query.subject,
                    query.course,
                    error,
                )
                continue

            old_sections = previous.get(query)
            if old_sections is None:
                logger.info(
                    "tracking %d Lecture sections for %s %s",
                    len(sections),
                    query.subject,
                    query.course,
                )
                print_sections(sections, query=query)
                if waitlist_registrar is not None:
                    for section in sections:
                        if section.waitlist_available:
                            logger.info(
                                "waitlist for CRN %s is already open on the initial check",
                                section.crn,
                            )
                            pending_waitlist_adds[(query, section.crn)] = (
                                AvailabilityOpening(
                                    section=section,
                                    pool="waitlist",
                                    previous_remaining=0,
                                    current_remaining=section.waitlist_remaining or 0,
                                )
                            )
            else:
                openings = availability_openings(old_sections, sections)
                if openings:
                    logger.info(
                        "detected %d new opening(s) for %s %s",
                        len(openings),
                        query.subject,
                        query.course,
                    )
                    print_openings(openings)
                    if waitlist_registrar is not None:
                        for opening in openings:
                            if opening.pool == "waitlist":
                                pending_waitlist_adds[(query, opening.section.crn)] = (
                                    opening
                                )
                    for notifier_index, notifier in enumerate(configured_notifiers):
                        for opening in openings:
                            key = (
                                notifier_index,
                                query.term,
                                opening.section.crn,
                                opening.pool,
                            )
                            pending = pending_notifications.get(key)
                            if pending is None:
                                pending_notifications[key] = (notifier, opening)
                            else:
                                pending_opening = pending[1]
                                pending_notifications[key] = (
                                    notifier,
                                    AvailabilityOpening(
                                        section=opening.section,
                                        pool=opening.pool,
                                        previous_remaining=(
                                            pending_opening.previous_remaining
                                        ),
                                        current_remaining=opening.current_remaining,
                                    ),
                                )
                else:
                    logger.info(
                        "no new opening detected for %s %s",
                        query.subject,
                        query.course,
                    )
            previous[query] = sections

        completed_queries: set[CourseQuery] = set()
        if waitlist_registrar is not None:
            for key, opening in tuple(pending_waitlist_adds.items()):
                query, crn = key
                if query in completed_queries:
                    del pending_waitlist_adds[key]
                    continue
                try:
                    result = waitlist_registrar.add_to_waitlist(query.term, crn)
                except WaitlistError as error:
                    logger.error(
                        "automatic waitlist attempt for CRN %s stopped: %s",
                        crn,
                        error,
                    )
                    del pending_waitlist_adds[key]
                    continue
                except MinervaError as error:
                    logger.warning(
                        "automatic waitlist attempt for CRN %s failed; will retry: %s",
                        crn,
                        error,
                    )
                    continue

                print(f"AUTO-WAITLIST {result.outcome}: CRN {crn} — {result.message}")
                if result.outcome is WaitlistOutcome.WAITLIST_FULL:
                    logger.warning(
                        "waitlist for CRN %s became full before submission", crn
                    )
                elif result.successful:
                    logger.info(
                        "automatic waitlist result for CRN %s: %s",
                        crn,
                        result.outcome,
                    )
                    completed_queries.add(query)
                    for notifier_index, notifier in enumerate(configured_notifiers):
                        # A successful automatic action supersedes the opening
                        # alert: only report the completed registration outcome.
                        pending_notifications.pop(
                            (
                                notifier_index,
                                query.term,
                                crn,
                                opening.pool,
                            ),
                            None,
                        )
                        notification_key = (notifier_index, query.term, crn)
                        pending_registration_notifications[notification_key] = (
                            notifier,
                            opening,
                            result,
                        )
                else:
                    logger.error(
                        "automatic waitlist rejected for CRN %s: %s",
                        crn,
                        result.message,
                    )
                del pending_waitlist_adds[key]

        if completed_queries:
            for query in completed_queries:
                logger.info(
                    "stopping polling for %s %s after successful registration outcome",
                    query.subject,
                    query.course,
                )
                active_queries.remove(query)
                previous.pop(query, None)
            for key in tuple(pending_waitlist_adds):
                if key[0] in completed_queries:
                    del pending_waitlist_adds[key]

        for key, (notifier, opening) in tuple(pending_notifications.items()):
            try:
                notifier.notify_opening(opening)
            except NotificationError as error:
                logger.warning(
                    "%s delivery failed; will retry: %s",
                    type(notifier).__name__,
                    error,
                )
            else:
                del pending_notifications[key]

        for key, (notifier, opening, result) in tuple(
            pending_registration_notifications.items()
        ):
            try:
                notifier.notify_registration(opening, result)
            except NotificationError as error:
                logger.warning(
                    "%s registration notification failed: %s",
                    type(notifier).__name__,
                    error,
                )
            else:
                del pending_registration_notifications[key]

        if once:
            return
        if not active_queries:
            logger.info("all watched courses completed; exiting")
            return
        logger.info("next cycle in %.1f seconds", interval)
        time.sleep(interval)


def poll_course(
    client: MinervaClient,
    query: CourseQuery,
    *,
    interval: float = 60,
    once: bool = False,
    notifiers: Iterable[OpeningNotifier] = (),
    waitlist_registrar: WaitlistRegistrar | None = None,
) -> None:
    """Backward-compatible single-course polling wrapper."""
    poll_courses(
        client,
        [query],
        interval=interval,
        once=once,
        notifiers=notifiers,
        waitlist_registrar=waitlist_registrar,
    )
