"""State-changing Minerva quick-add and waitlist operations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from .client import MinervaClient, MinervaError

QUICK_ADD_PATH = "/pban1/bwskfreg.P_AltPin"
REGISTRATION_PATH = "/pban1/bwckcoms.P_Regs"
logger = logging.getLogger(__name__)


class WaitlistError(MinervaError):
    """The waitlist workflow could not safely be completed or interpreted."""


class WaitlistOutcome(StrEnum):
    ADDED = "added-to-waitlist"
    ALREADY_ADDED = "already-on-waitlist"
    WAITLIST_FULL = "waitlist-full"
    REGISTERED = "registered-directly"
    REJECTED = "rejected"


@dataclass(frozen=True)
class WaitlistResult:
    crn: str
    outcome: WaitlistOutcome
    message: str

    @property
    def successful(self) -> bool:
        return self.outcome in {
            WaitlistOutcome.ADDED,
            WaitlistOutcome.ALREADY_ADDED,
            WaitlistOutcome.REGISTERED,
        }


def _form_for_registration(html: str) -> tuple[BeautifulSoup, Tag]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find(
        "form", attrs={"action": re.compile(r"bwckcoms\.P_Regs", re.IGNORECASE)}
    )
    if not isinstance(form, Tag):
        title = (
            " ".join(soup.title.get_text(" ", strip=True).split())
            if soup.title
            else "unknown"
        )
        actions = sorted(
            {
                str(candidate.get("action"))
                for candidate in soup.find_all("form")
                if candidate.get("action")
            }
        )
        raise WaitlistError(
            f"Minerva page {title!r} did not contain the registration form; "
            f"form actions: {actions or ['none']}"
        )
    return soup, form


def _select_term_form(html: str, term: str) -> list[tuple[str, str]] | None:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find(
        "form", attrs={"action": re.compile(r"bwskfreg\.P_AltPin", re.IGNORECASE)}
    )
    if not isinstance(form, Tag):
        return None
    selector = form.find("select", attrs={"name": "term_in"})
    if not isinstance(selector, Tag):
        return None
    option = selector.find("option", attrs={"value": term})
    if not isinstance(option, Tag):
        available = [str(item.get("value")) for item in selector.find_all("option")]
        raise WaitlistError(
            f"term {term} is not available for registration; available terms: {available}"
        )

    fields: list[tuple[str, str]] = []
    for hidden in form.find_all("input", attrs={"type": "hidden"}):
        name = hidden.get("name")
        if name:
            fields.append((str(name), str(hidden.get("value", ""))))
    fields.append(("term_in", term))
    return fields


def _input_value(container: Tag, name: str) -> str | None:
    field = container.find("input", attrs={"name": name})
    if isinstance(field, Tag):
        value = field.get("value")
        return str(value) if value is not None else ""
    return None


def _schedule_status(soup: BeautifulSoup, crn: str) -> str | None:
    tables = soup.find_all(
        "table", attrs={"summary": re.compile("Current Schedule", re.IGNORECASE)}
    )
    for table in tables:
        for row in table.find_all("tr"):
            if _input_value(row, "CRN_IN") == crn:
                cell = row.find("td")
                return (
                    " ".join(cell.get_text(" ", strip=True).split())
                    if isinstance(cell, Tag)
                    else ""
                )
    return None


def _registration_error(soup: BeautifulSoup, crn: str) -> str | None:
    for table in soup.find_all("table"):
        summary = str(table.get("summary", ""))
        if "Registration Errors" not in summary:
            continue
        for row in table.find_all("tr"):
            hidden_crn = _input_value(row, "CRN_IN")
            cells = row.find_all("td", recursive=False)
            cell_texts = [
                " ".join(cell.get_text(" ", strip=True).split()) for cell in cells
            ]
            if hidden_crn == crn or crn in cell_texts:
                return cell_texts[0] if cell_texts else "Registration rejected"
    return None


def _waitlist_select(form: Tag, crn: str) -> Tag | None:
    for select in form.find_all("select", attrs={"name": "RSTS_IN"}):
        if not select.find("option", attrs={"value": "LW"}):
            continue
        row = select.find_parent("tr")
        if isinstance(row, Tag) and _input_value(row, "CRN_IN") == crn:
            return select
    return None


def _serialize_registration_form(
    form: Tag,
    *,
    quick_add_crn: str | None = None,
    waitlist_select: Tag | None = None,
) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    quick_add_used = False

    for control in form.find_all(["input", "select", "textarea"]):
        if control.has_attr("disabled"):
            continue
        name_value = control.get("name")
        if not name_value:
            continue
        name = str(name_value)

        if control.name == "input":
            input_type = str(control.get("type", "text")).lower()
            if input_type in {"submit", "reset", "button", "image", "file"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            value = str(control.get("value", ""))
            if (
                quick_add_crn is not None
                and not quick_add_used
                and input_type == "text"
                and name == "CRN_IN"
            ):
                value = quick_add_crn
                quick_add_used = True
            fields.append((name, value))
            continue

        if control.name == "textarea":
            fields.append((name, control.get_text()))
            continue

        if control is waitlist_select:
            fields.append((name, "LW"))
            continue
        options = control.find_all("option")
        selected = [option for option in options if option.has_attr("selected")]
        chosen = selected or options[:1]
        if not control.has_attr("multiple"):
            chosen = chosen[:1]
        for option in chosen:
            fields.append((name, str(option.get("value", option.get_text(strip=True)))))

    if quick_add_crn is not None and not quick_add_used:
        raise WaitlistError("registration form had no quick-add CRN field")
    fields.append(("REG_BTN", "Submit Changes"))
    return fields


def _post_registration(
    client: MinervaClient, fields: list[tuple[str, str]], *, referer: str
) -> str:
    response = client.request(
        REGISTRATION_PATH,
        method="POST",
        data=urlencode(fields).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://horizon.mcgill.ca",
            "Referer": f"https://horizon.mcgill.ca{referer}",
        },
    )
    return response.text


class WaitlistRegistrar:
    """Execute Banner's quick-add followed by explicit add-to-waitlist flow."""

    def __init__(self, client: MinervaClient) -> None:
        self.client = client

    def add_to_waitlist(self, term: str, crn: str) -> WaitlistResult:
        if not re.fullmatch(r"\d{6}", term):
            raise ValueError("term must be a six-digit Banner term")
        if not re.fullmatch(r"\d{1,5}", crn):
            raise ValueError("CRN must contain 1-5 digits")

        logger.info("loading Quick Add form for CRN %s", crn)
        initial = self.client.request(QUICK_ADD_PATH)
        initial_html = initial.text
        term_fields = _select_term_form(initial_html, term)
        if term_fields is not None:
            logger.info("selecting registration term %s", term)
            selected = self.client.request(
                QUICK_ADD_PATH,
                method="POST",
                data=urlencode(term_fields).encode("utf-8"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://horizon.mcgill.ca",
                    "Referer": f"https://horizon.mcgill.ca{QUICK_ADD_PATH}",
                },
            )
            initial_html = selected.text

        initial_soup, initial_form = _form_for_registration(initial_html)
        form_term = _input_value(initial_form, "term_in")
        if form_term != term:
            raise WaitlistError(
                f"registration form is set to term {form_term or 'unknown'}, not {term}"
            )

        existing = _schedule_status(initial_soup, crn)
        if existing:
            if "waitlist" in existing.lower():
                return WaitlistResult(crn, WaitlistOutcome.ALREADY_ADDED, existing)
            return WaitlistResult(crn, WaitlistOutcome.REGISTERED, existing)

        logger.warning(
            "submitting Quick Add for CRN %s; Banner may register directly if a seat is open",
            crn,
        )
        first_html = _post_registration(
            self.client,
            _serialize_registration_form(initial_form, quick_add_crn=crn),
            referer=QUICK_ADD_PATH,
        )
        first_soup, first_form = _form_for_registration(first_html)

        direct_status = _schedule_status(first_soup, crn)
        if direct_status:
            if "waitlist" in direct_status.lower():
                return WaitlistResult(crn, WaitlistOutcome.ADDED, direct_status)
            return WaitlistResult(crn, WaitlistOutcome.REGISTERED, direct_status)

        error = _registration_error(first_soup, crn)
        if error and "waitlist full" in error.lower():
            return WaitlistResult(crn, WaitlistOutcome.WAITLIST_FULL, error)

        waitlist_control = _waitlist_select(first_form, crn)
        if waitlist_control is None:
            return WaitlistResult(
                crn,
                WaitlistOutcome.REJECTED,
                error or "Minerva did not offer an Add to Waitlist action",
            )

        logger.info("Minerva offered waitlisting for CRN %s; submitting LW action", crn)
        second_html = _post_registration(
            self.client,
            _serialize_registration_form(first_form, waitlist_select=waitlist_control),
            referer=REGISTRATION_PATH,
        )
        second_soup, _ = _form_for_registration(second_html)
        final_status = _schedule_status(second_soup, crn)
        if final_status and "waitlist" in final_status.lower():
            return WaitlistResult(crn, WaitlistOutcome.ADDED, final_status)
        if final_status:
            return WaitlistResult(crn, WaitlistOutcome.REGISTERED, final_status)

        error = _registration_error(second_soup, crn)
        if error and "waitlist full" in error.lower():
            return WaitlistResult(crn, WaitlistOutcome.WAITLIST_FULL, error)
        return WaitlistResult(
            crn,
            WaitlistOutcome.REJECTED,
            error or "Minerva did not confirm the waitlist addition",
        )
