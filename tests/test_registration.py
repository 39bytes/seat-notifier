from __future__ import annotations

import unittest
from email.message import Message
from typing import Any, cast
from urllib.parse import parse_qsl

from seat_notifier.client import MinervaResponse
from seat_notifier.registration import (
    WaitlistError,
    WaitlistOutcome,
    WaitlistRegistrar,
)


def term_selection_page() -> str:
    return """
    <html><head><title>Select Term</title></head><body>
    <form action="/pban1/bwskfreg.P_AltPin" method="post">
      <select name="term_in">
        <option value="202701">Winter 2027</option>
        <option value="202609">Fall 2026</option>
        <option value="202605" selected>Summer 2026</option>
      </select>
      <input type="submit" value="Submit">
    </form></body></html>
    """


def registration_page(*body: str, term: str = "202609") -> str:
    return f"""
    <html><body><form action="/pban1/bwckcoms.P_Regs" method="post">
      <input type="hidden" name="term_in" value="{term}">
      <input type="hidden" name="RSTS_IN" value="DUMMY">
      <input type="hidden" name="CRN_IN" value="DUMMY">
      {"".join(body)}
      <table class="dataentrytable" summary="Add Classes Data Entry"><tr><td>
        <input type="hidden" name="RSTS_IN" value="RW">
        <input type="text" name="CRN_IN" value="">
        <input type="hidden" name="assoc_term_in" value="">
        <input type="hidden" name="start_date_in" value="">
        <input type="hidden" name="end_date_in" value="">
      </td><td>
        <input type="hidden" name="RSTS_IN" value="RW">
        <input type="text" name="CRN_IN" value="">
        <input type="hidden" name="assoc_term_in" value="">
        <input type="hidden" name="start_date_in" value="">
        <input type="hidden" name="end_date_in" value="">
      </td></tr></table>
      <input type="hidden" name="regs_row" value="1">
      <input type="hidden" name="wait_row" value="0">
      <input type="hidden" name="add_row" value="2">
      <input type="submit" name="REG_BTN" value="Submit Changes">
    </form></body></html>
    """


EXISTING_SCHEDULE = """
<table summary="Current Schedule">
<tr><th>Status</th><th>Action</th><th>CRN</th></tr>
<tr>
  <td><input type="hidden" name="MESG" value="DUMMY">Web Registered</td>
  <td><select name="RSTS_IN"><option value="" selected>None</option><option value="DW">Drop</option></select></td>
  <td><input type="hidden" name="assoc_term_in" value="202609">
      <input type="hidden" name="CRN_IN" value="1111">1111
      <input type="hidden" name="start_date_in" value="08/31/2026">
      <input type="hidden" name="end_date_in" value="12/04/2026"></td>
  <td><input type="hidden" name="SUBJ" value="COMP"></td>
  <td><input type="hidden" name="CRSE" value="100"></td>
  <td><input type="hidden" name="GMOD" value="Standard"></td>
</tr></table>
"""

WAITLIST_PROMPT = """
<table summary="Current Schedule"></table>
<table summary="This layout table is used to present Registration Errors.">
<tr><th>Status</th><th>Action</th><th>CRN</th></tr>
<tr>
  <td><input type="hidden" name="MESG" value="Closed - join waitlist">Closed - join waitlist</td>
  <td><select name="RSTS_IN"><option value="" selected>None</option><option value="LW">Add to Waitlist</option></select></td>
  <td><input type="hidden" name="assoc_term_in" value="202609">
      <input type="hidden" name="CRN_IN" value="2359">2359
      <input type="hidden" name="start_date_in" value="08/31/2026">
      <input type="hidden" name="end_date_in" value="12/04/2026"></td>
  <td><input type="hidden" name="SUBJ" value="COMP"></td>
  <td><input type="hidden" name="CRSE" value="598"></td>
  <td><input type="hidden" name="SEC" value="001"></td>
  <td><input type="hidden" name="LEVL" value="UG"></td>
  <td><input type="hidden" name="CRED" value="    4.000"></td>
  <td><input type="hidden" name="GMOD" value="Standard"></td>
  <td><input type="hidden" name="TITLE" value="Example Course"></td>
</tr></table>
"""

WAITLIST_SUCCESS = """
<table summary="Current Schedule"><tr>
  <td><input type="hidden" name="MESG" value="DUMMY">(Add(ed) to Waitlist) today</td>
  <td><select name="RSTS_IN"><option value="" selected>None</option></select></td>
  <td><input type="hidden" name="assoc_term_in" value="202609">
      <input type="hidden" name="CRN_IN" value="2359">2359</td>
</tr></table>
"""

WAITLIST_FULL = """
<table summary="Current Schedule"></table>
<table summary="This layout table is used to present Registration Errors.">
<tr><th>Status</th><th>CRN</th></tr>
<tr><td>Open - Waitlist Full</td><td>2359</td></tr>
</table>
"""


class FakeClient:
    def __init__(self, pages: list[str]):
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, path: str, **kwargs: Any) -> MinervaResponse:
        self.calls.append((path, kwargs))
        headers = Message()
        headers["Content-Type"] = "text/html; charset=UTF-8"
        return MinervaResponse(
            200, "https://horizon.mcgill.ca" + path, headers, self.pages.pop(0).encode()
        )


class WaitlistRegistrarTests(unittest.TestCase):
    def test_selects_registration_term_after_fresh_login(self) -> None:
        client = FakeClient(
            [term_selection_page(), registration_page(WAITLIST_SUCCESS)]
        )

        result = WaitlistRegistrar(cast(Any, client)).add_to_waitlist("202609", "2359")

        self.assertEqual(result.outcome, WaitlistOutcome.ALREADY_ADDED)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][0], "/pban1/bwskfreg.P_AltPin")
        fields = parse_qsl(client.calls[1][1]["data"].decode())
        self.assertEqual(fields, [("term_in", "202609")])

    def test_executes_quick_add_then_lw_and_verifies_schedule(self) -> None:
        client = FakeClient(
            [
                registration_page(EXISTING_SCHEDULE),
                registration_page(EXISTING_SCHEDULE, WAITLIST_PROMPT),
                registration_page(EXISTING_SCHEDULE, WAITLIST_SUCCESS),
            ]
        )

        with self.assertLogs("seat_notifier.registration", level="WARNING"):
            result = WaitlistRegistrar(cast(Any, client)).add_to_waitlist(
                "202609", "2359"
            )

        self.assertEqual(result.outcome, WaitlistOutcome.ADDED)
        self.assertTrue(result.successful)
        self.assertEqual(len(client.calls), 3)

        first_fields = parse_qsl(
            client.calls[1][1]["data"].decode(), keep_blank_values=True
        )
        self.assertIn(("CRN_IN", "1111"), first_fields)
        self.assertIn(("CRN_IN", "2359"), first_fields)
        self.assertIn(("RSTS_IN", "RW"), first_fields)
        self.assertEqual(first_fields[-1], ("REG_BTN", "Submit Changes"))

        second_fields = parse_qsl(
            client.calls[2][1]["data"].decode(), keep_blank_values=True
        )
        self.assertIn(("CRN_IN", "1111"), second_fields)
        self.assertIn(("CRN_IN", "2359"), second_fields)
        self.assertIn(("RSTS_IN", "LW"), second_fields)
        self.assertEqual(second_fields[-1], ("REG_BTN", "Submit Changes"))

    def test_reports_waitlist_full_after_lw_submission(self) -> None:
        client = FakeClient(
            [
                registration_page(EXISTING_SCHEDULE),
                registration_page(EXISTING_SCHEDULE, WAITLIST_PROMPT),
                registration_page(EXISTING_SCHEDULE, WAITLIST_FULL),
            ]
        )

        with self.assertLogs("seat_notifier.registration", level="WARNING"):
            result = WaitlistRegistrar(cast(Any, client)).add_to_waitlist(
                "202609", "2359"
            )

        self.assertEqual(result.outcome, WaitlistOutcome.WAITLIST_FULL)
        self.assertFalse(result.successful)
        self.assertIn("Waitlist Full", result.message)

    def test_refuses_to_submit_again_when_already_waitlisted(self) -> None:
        client = FakeClient([registration_page(WAITLIST_SUCCESS)])

        result = WaitlistRegistrar(cast(Any, client)).add_to_waitlist("202609", "2359")

        self.assertEqual(result.outcome, WaitlistOutcome.ALREADY_ADDED)
        self.assertEqual(len(client.calls), 1)

    def test_refuses_wrong_registration_term(self) -> None:
        client = FakeClient([registration_page(term="202701")])

        with self.assertRaisesRegex(WaitlistError, "not 202609"):
            WaitlistRegistrar(cast(Any, client)).add_to_waitlist("202609", "2359")

        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
