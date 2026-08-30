from __future__ import annotations

import argparse
import logging
import os
import re
from http.cookiejar import CookieJar
from pathlib import Path

from dotenv import load_dotenv

from .auth import BrowserAuthenticationError, GeckoAuthenticator
from .client import (
    AuthenticationRequired,
    MinervaClient,
    MinervaError,
    cookies_from_header,
    cookies_from_webdriver_json,
)
from .courses import CourseQuery, CourseSearchError, poll_courses
from .health import start_health_server
from .notifications import (
    DEFAULT_NTFY_SERVER,
    DEFAULT_REGISTRATION_URL,
    DbusNotifier,
    NotificationError,
    NtfyNotifier,
)
from .registration import WaitlistRegistrar

logger = logging.getLogger(__name__)


def _load_root_dotenv() -> Path | None:
    """Load .env beside the nearest parent pyproject.toml."""
    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        if (directory / "pyproject.toml").is_file():
            dotenv_path = directory / ".env"
            if dotenv_path.is_file():
                load_dotenv(dotenv_path, override=False)
                return dotenv_path
            return None
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll McGill Minerva course availability"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--webdriver-cookies",
        metavar="PATH",
        type=Path,
        help="JSON file produced by Selenium driver.get_cookies()",
    )
    source.add_argument(
        "--cookie-env",
        default="MINERVA_COOKIE",
        metavar="NAME",
        help="environment variable containing a raw Cookie header (default: MINERVA_COOKIE)",
    )
    parser.add_argument(
        "--auto-login",
        action="store_true",
        help="use Firefox and MINERVA_USERNAME/PASSWORD/TOTP_SECRET when the session expires",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show Firefox during automatic login (default: headless)",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=120,
        metavar="SECONDS",
        help="automatic-login timeout (default: 120)",
    )
    logging_options = parser.add_mutually_exclusive_group()
    logging_options.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show detailed HTTP and browser diagnostics",
    )
    logging_options.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only log warnings and errors",
    )
    parser.add_argument(
        "--term",
        metavar="YYYYTT",
        help="Banner term (default: MINERVA_TERM), for example 202609",
    )
    parser.add_argument(
        "--subject", help="course subject for poll mode, for example COMP"
    )
    parser.add_argument(
        "--course",
        action="append",
        help=(
            "course number with --subject, or SUBJECT:NUMBER; repeat to poll multiple courses "
            "(default: MINERVA_COURSES)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60,
        metavar="SECONDS",
        help="polling interval (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="check once and exit instead of polling continuously",
    )
    parser.add_argument(
        "--ntfy-topic",
        help="ntfy topic for opening alerts (default: NTFY_TOPIC)",
    )
    parser.add_argument(
        "--ntfy-server",
        help="ntfy server URL (default: NTFY_SERVER or https://ntfy.sh)",
    )
    parser.add_argument(
        "--no-notify",
        "--no-ntfy",
        action="store_true",
        help="disable ntfy notifications",
    )
    parser.add_argument(
        "--no-dbus",
        action="store_true",
        help="disable local D-Bus desktop notifications",
    )
    parser.add_argument(
        "--auto-waitlist",
        action="store_true",
        help="automatically submit Add to Waitlist when a waitlist opening is detected",
    )
    parser.add_argument("--crn", help="CRN for the waitlist-add command")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the state-changing waitlist-add command",
    )
    parser.add_argument(
        "--health-host",
        help="health server bind address (default: HEALTH_HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        help="health server port (default: PORT, HEALTH_PORT, or 8080)",
    )
    parser.add_argument(
        "--no-health-server",
        action="store_true",
        help="disable the poll-mode HTTP health server",
    )
    parser.add_argument(
        "command",
        choices=("probe", "cookie-names", "poll", "notify-test", "waitlist-add"),
        nargs="?",
        default="probe",
    )
    return parser


def _ntfy_notifier(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> NtfyNotifier:
    topic = args.ntfy_topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        parser.error("ntfy requires NTFY_TOPIC or --ntfy-topic")
    try:
        return NtfyNotifier(
            topic,
            server=(
                args.ntfy_server or os.environ.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER
            ),
            token=os.environ.get("NTFY_TOKEN"),
            click_url=os.environ.get(
                "MINERVA_REGISTRATION_URL", DEFAULT_REGISTRATION_URL
            ),
        )
    except ValueError as error:
        parser.error(str(error))


def _environment_flag(name: str) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _poll_queries(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[CourseQuery, ...]:
    term = args.term or os.environ.get("MINERVA_TERM")
    if not term:
        parser.error("poll requires --term or MINERVA_TERM")

    if args.course:
        course_specs = args.course
    else:
        configured = os.environ.get("MINERVA_COURSES", "")
        course_specs = [
            spec for spec in re.split(r"[,\s]+", configured.strip()) if spec
        ]
    if not course_specs:
        parser.error("poll requires --course or MINERVA_COURSES")

    queries: list[CourseQuery] = []
    for spec in course_specs:
        if ":" in spec:
            if args.subject:
                parser.error(
                    "do not combine --subject with SUBJECT:NUMBER course specifications"
                )
            subject, course = spec.split(":", 1)
        else:
            if not args.subject:
                parser.error(
                    "use --subject with course numbers, or use --course SUBJECT:NUMBER"
                )
            subject, course = args.subject, spec
        try:
            queries.append(CourseQuery(term, subject, course))
        except ValueError as error:
            parser.error(str(error))
    return tuple(dict.fromkeys(queries))


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    level = (
        logging.DEBUG
        if args.verbose
        else logging.WARNING
        if args.quiet
        else logging.INFO
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    dotenv_path = _load_root_dotenv()
    if dotenv_path:
        logger.info("loaded configuration from %s", dotenv_path)
    else:
        logger.debug("no project-root .env file found")

    if args.command == "notify-test":
        if args.no_notify and args.no_dbus:
            parser.error("notify-test cannot disable both ntfy and D-Bus")
        failures: list[str] = []
        if not args.no_notify:
            try:
                _ntfy_notifier(args, parser).send_test()
            except NotificationError as error:
                failures.append(f"ntfy: {error}")
        if not args.no_dbus:
            try:
                DbusNotifier().send_test()
            except NotificationError as error:
                failures.append(f"D-Bus: {error}")
        if failures:
            raise SystemExit(f"test notification failed: {'; '.join(failures)}")
        print("Test notifications delivered.")
        return

    raw_cookie = os.environ.get(args.cookie_env)
    if args.webdriver_cookies and args.webdriver_cookies.exists():
        jar = cookies_from_webdriver_json(args.webdriver_cookies)
        logger.info(
            "loaded %d cached cookies from %s", len(jar), args.webdriver_cookies
        )
    elif raw_cookie:
        jar = cookies_from_header(raw_cookie)
        logger.info("loaded %d cookies from %s", len(jar), args.cookie_env)
    elif args.auto_login:
        jar = CookieJar()
        logger.info(
            "no existing cookies found; browser authentication will be required"
        )
    elif args.webdriver_cookies:
        raise SystemExit(f"cookie file does not exist: {args.webdriver_cookies}")
    else:
        raise SystemExit(
            f"set {args.cookie_env}, use --webdriver-cookies PATH, or enable --auto-login"
        )

    authenticator = None
    if args.auto_login:
        try:
            authenticator = GeckoAuthenticator.from_environment(
                headless=not args.headed,
                timeout=args.login_timeout,
                cookie_cache=args.webdriver_cookies,
            )
        except ValueError as error:
            raise SystemExit(f"automatic login configuration error: {error}") from error
        logger.info("automatic browser login is enabled")

    client = MinervaClient(jar, authenticator=authenticator)
    if args.command == "cookie-names":
        print("\n".join(client.cookie_names()))
        return

    if args.command == "waitlist-add":
        waitlist_term = args.term or os.environ.get("MINERVA_TERM")
        if not waitlist_term or not args.crn:
            parser.error("waitlist-add requires --term/MINERVA_TERM and --crn")
        if not args.yes:
            parser.error(
                "waitlist-add can change registration; rerun with --yes after verifying the term and CRN"
            )
        try:
            result = WaitlistRegistrar(client).add_to_waitlist(waitlist_term, args.crn)
        except (AuthenticationRequired, BrowserAuthenticationError) as error:
            raise SystemExit(f"authentication failed: {error}") from error
        except (MinervaError, ValueError) as error:
            raise SystemExit(f"waitlist addition failed: {error}") from error
        print(f"{result.outcome}: CRN {result.crn} — {result.message}")
        if not result.successful:
            raise SystemExit(2)
        return

    if args.command == "poll":
        queries = _poll_queries(args, parser)
        notifiers = []
        if not args.no_notify:
            notifiers.append(_ntfy_notifier(args, parser))
            logger.info("ntfy opening notifications are enabled")
        else:
            logger.info("ntfy opening notifications are disabled")
        if not args.no_dbus:
            notifiers.append(DbusNotifier())
            logger.info("D-Bus desktop notifications are enabled")
        else:
            logger.info("D-Bus desktop notifications are disabled")

        try:
            auto_waitlist = args.auto_waitlist or _environment_flag(
                "MINERVA_AUTO_WAITLIST"
            )
        except ValueError as error:
            parser.error(str(error))
        waitlist_registrar = WaitlistRegistrar(client) if auto_waitlist else None
        if waitlist_registrar:
            logger.warning(
                "automatic waitlist submission is enabled and may change registration"
            )

        health_server = None
        if not args.no_health_server:
            configured_port = (
                str(args.health_port)
                if args.health_port is not None
                else os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "8080"
            )
            try:
                health_port = int(configured_port)
                health_server = start_health_server(
                    host=args.health_host or os.environ.get("HEALTH_HOST", "0.0.0.0"),
                    port=health_port,
                    notifiers=tuple(notifiers),
                )
            except (OSError, ValueError) as error:
                raise SystemExit(f"health server failed to start: {error}") from error

        try:
            poll_courses(
                client,
                queries,
                interval=args.interval,
                once=args.once,
                notifiers=notifiers,
                waitlist_registrar=waitlist_registrar,
            )
        except KeyboardInterrupt:
            logger.info("polling stopped")
        except (AuthenticationRequired, BrowserAuthenticationError) as error:
            raise SystemExit(f"authentication failed: {error}") from error
        except (CourseSearchError, MinervaError, ValueError) as error:
            raise SystemExit(f"course polling failed: {error}") from error
        finally:
            if health_server is not None:
                health_server.shutdown()
                health_server.server_close()
        return

    try:
        response = client.probe()
    except (AuthenticationRequired, BrowserAuthenticationError) as error:
        raise SystemExit(f"authentication failed: {error}") from error
    print(
        f"authenticated Minerva response: HTTP {response.status} ({len(response.body)} bytes)"
    )
