from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from seat_notifier import (
    _environment_flag,
    _load_root_dotenv,
    _parser,
    _poll_queries,
    main,
)


class CliTests(unittest.TestCase):
    def test_reads_explicit_environment_flag(self) -> None:
        with patch.dict(os.environ, {"MINERVA_AUTO_WAITLIST": "true"}, clear=True):
            self.assertTrue(_environment_flag("MINERVA_AUTO_WAITLIST"))

        with patch.dict(os.environ, {"MINERVA_AUTO_WAITLIST": "false"}, clear=True):
            self.assertFalse(_environment_flag("MINERVA_AUTO_WAITLIST"))

    def test_reads_poll_term_and_courses_from_environment(self) -> None:
        parser = _parser()
        args = parser.parse_args(["poll"])

        with patch.dict(
            os.environ,
            {
                "MINERVA_TERM": "202609",
                "MINERVA_COURSES": "COMP:350, MATH:240  ECSE:205",
            },
            clear=True,
        ):
            queries = _poll_queries(args, parser)

        self.assertEqual(
            [(query.term, query.subject, query.course) for query in queries],
            [
                ("202609", "COMP", "350"),
                ("202609", "MATH", "240"),
                ("202609", "ECSE", "205"),
            ],
        )

    def test_parses_multiple_fully_qualified_courses(self) -> None:
        parser = _parser()
        args = parser.parse_args(
            [
                "poll",
                "--term",
                "202609",
                "--course",
                "COMP:350",
                "--course",
                "MATH:240",
            ]
        )

        queries = _poll_queries(args, parser)

        self.assertEqual(
            [(query.subject, query.course) for query in queries],
            [("COMP", "350"), ("MATH", "240")],
        )

    def test_parses_multiple_courses_with_shared_subject(self) -> None:
        parser = _parser()
        args = parser.parse_args(
            [
                "poll",
                "--term",
                "202609",
                "--subject",
                "COMP",
                "--course",
                "350",
                "--course",
                "551",
            ]
        )

        queries = _poll_queries(args, parser)

        self.assertEqual(
            [(query.subject, query.course) for query in queries],
            [("COMP", "350"), ("COMP", "551")],
        )

    def test_notify_test_does_not_require_minerva_cookies(self) -> None:
        environment = {
            "NTFY_TOPIC": "test-topic",
            "MINERVA_COOKIE": "",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["seat-notifier", "notify-test"]),
            patch("seat_notifier._load_root_dotenv", return_value=None),
            patch("seat_notifier.logging.basicConfig"),
            patch(
                "seat_notifier.notifications.NtfyNotifier._post",
                new_callable=AsyncMock,
            ) as mocked_post,
            patch("seat_notifier.DbusNotifier.send_test") as mocked_dbus,
            redirect_stdout(StringIO()) as output,
        ):
            main()

        self.assertEqual(mocked_post.await_count, 1)
        self.assertEqual(mocked_dbus.call_count, 1)
        self.assertIn("Test notifications delivered", output.getvalue())


class DotenvTests(unittest.TestCase):
    def test_loads_dotenv_from_project_root_without_overriding_environment(
        self,
    ) -> None:
        loaded_name = "SEAT_NOTIFIER_DOTENV_TEST_LOADED"
        existing_name = "SEAT_NOTIFIER_DOTENV_TEST_EXISTING"
        previous_cwd = Path.cwd()
        previous_loaded = os.environ.pop(loaded_name, None)
        previous_existing = os.environ.get(existing_name)
        os.environ[existing_name] = "from-environment"

        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                nested = root / "some" / "nested" / "directory"
                nested.mkdir(parents=True)
                (root / "pyproject.toml").write_text("[project]\nname='test'\n")
                (root / ".env").write_text(
                    f"{loaded_name}=from-dotenv\n{existing_name}=from-dotenv\n"
                )
                os.chdir(nested)

                loaded_path = _load_root_dotenv()

                self.assertEqual(loaded_path, root / ".env")
                self.assertEqual(os.environ[loaded_name], "from-dotenv")
                self.assertEqual(os.environ[existing_name], "from-environment")
        finally:
            os.chdir(previous_cwd)
            if previous_loaded is None:
                os.environ.pop(loaded_name, None)
            else:
                os.environ[loaded_name] = previous_loaded
            if previous_existing is None:
                os.environ.pop(existing_name, None)
            else:
                os.environ[existing_name] = previous_existing


if __name__ == "__main__":
    unittest.main()
