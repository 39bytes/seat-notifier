# seat-notifier

A work-in-progress client for polling course availability in McGill Minerva.

## What matters in a captured Minerva request

For this request:

```text
GET https://horizon.mcgill.ca/pban1/twbkwbis.P_GenMenu?name=bmenu.P_MainMnu
```

| Part | Importance | Notes |
|---|---:|---|
| Method, host, path, query | Required | These identify the Banner procedure and its arguments. |
| `Cookie` | Required for authenticated pages | Keep cookies in a cookie jar and accept replacements from every response. Do not select one cookie prematurely. |
| `User-Agent` | Sometimes useful | A simple stable value works currently. Reuse the browser value if a gateway later rejects it. |
| `Accept` | Optional | Included by this client; not authentication-related. |
| `Referer` | Not required | The captured value contains a signed, short-lived SAML request. Do not save or replay it. |
| `Host` | Automatic | The HTTP library derives it from the URL. |
| `Accept-Encoding` | Automatic/optional | Let the HTTP library negotiate and decode compression. |
| `Connection`, `Priority`, `Upgrade-Insecure-Requests` | Not required | Browser transport/navigation hints. |
| `Sec-Fetch-*` | Not required | Browser context metadata, not credentials. |
| `Accept-Language` | Not required | It can affect presentation, but not authentication. |

### Cookie interpretation

The exact minimum cookie set should not be hard-coded:

- `SESSID` appears to be the authenticated Banner/Minerva session and is the most likely essential cookie.
- `BIGipServer...horizon_ban8x_ssb_pool` is F5 load-balancer affinity and may be needed to reach the server holding that session.
- `TS01...` is a gateway/WAF cookie and can be replaced by the server.
- `JSESSIONID`, `commonAuthId`, `samlssoTokenId`, and `IDMSESSID` belong to the SSO flow. They may not all be needed after Banner creates `SESSID`, but retaining them is harmless when requests are restricted to `horizon.mcgill.ca`.
- Other `BIGipServer...` cookies select SSO/dashboard backend pools and are probably irrelevant to ordinary Banner requests.
- `TESTID` is not an authentication credential.

A session can depend on affinity, gateway policy, and cookies added or rotated after this one request. The robust approach is therefore to import all cookies from the authenticated browser and then use a persistent cookie jar. Once that works, a local cookie-removal test can establish the minimum set, but doing so is not necessary for the client.

**Never commit, print, or send captured cookies.** A raw `Cookie` header grants access like a password until it expires. Log out after accidentally disclosing one.

## Current client

The client imports either a temporary raw header or Selenium/WebDriver cookie JSON. It only sends cookies back to HTTPS on `horizon.mcgill.ca`. It also detects the Minerva login form because an expired or absent session returns an HTTP `200` login page rather than `401`.

With `--auto-login`, an authenticated request follows this sequence:

1. send the request using the current cookie jar;
2. if Minerva returns its login form, launch an isolated Firefox through Geckodriver;
3. complete Microsoft sign-in with the configured username, password, and TOTP secret;
4. replace the HTTP client's cookies with the fresh browser cookies; and
5. retry the original request once.

Firefox is not launched while the existing session remains valid.

### Probe with a temporary environment variable

```bash
export MINERVA_COOKIE='name=value; other=value'
uv run seat-notifier probe
unset MINERVA_COOKIE
```

List cookie **names** without exposing values:

```bash
uv run seat-notifier cookie-names
```

Putting a secret directly on a command line is discouraged because it can enter shell history or process listings.

### Automatic login with Geckodriver

Requirements:

- Firefox and `geckodriver` available on `PATH` (or set `GECKODRIVER_PATH`);
- `MINERVA_USERNAME`;
- `MINERVA_PASSWORD`; and
- `MINERVA_TOTP_SECRET`, which is the Base32 authenticator seed—not a current six-digit code.

The CLI automatically searches upward from the working directory for the nearest `pyproject.toml` and loads `.env` beside it. Existing process environment variables take precedence. Start from the provided template and restrict its permissions:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env, then:
uv run seat-notifier --auto-login probe
```

Alternatively, export the three variables through your shell or secret manager. For a one-process session with no cookie written to disk:

```bash
export MINERVA_USERNAME='firstname.lastname@mail.mcgill.ca'
export MINERVA_PASSWORD='...'
export MINERVA_TOTP_SECRET='...'
uv run seat-notifier --auto-login probe
unset MINERVA_USERNAME MINERVA_PASSWORD MINERVA_TOTP_SECRET
```

To cache refreshed cookies between invocations, pass a path in a private runtime directory:

```bash
uv run seat-notifier \
  --auto-login \
  --webdriver-cookies "$XDG_RUNTIME_DIR/minerva-cookies.json" \
  probe
```

A missing cache file is allowed with `--auto-login`. The client creates it atomically with mode `0600`, loads it on the next run, and only opens Firefox after those cookies expire. Add `--headed` to show Firefox while diagnosing a changed Microsoft login page.

### Logging

Normal runs log major cookie, session, and browser-authentication steps to stderr without logging cookie values, credentials, or TOTP codes. Use `--verbose` for HTTP request/response and browser cleanup details, or `--quiet` to show only warnings and errors:

```bash
uv run seat-notifier --auto-login --verbose probe
uv run seat-notifier --auto-login --quiet probe
```

Environment variables are convenient but can be exposed to child processes and privileged local users. Prefer injecting them from your system's credential/secret manager. Never put credentials or the TOTP seed in source code, `.env` files committed to Git, command-line arguments, logs, or screenshots. Use this only in accordance with McGill's account and MFA policies.

## Polling course availability

Configure the term and fully-qualified courses in the root `.env`. Separate courses with commas and/or whitespace:

```dotenv
MINERVA_TERM=202609
MINERVA_COURSES=COMP:350,MATH:240 ECSE:205
```

Then start polling without repeating them on the command line:

```bash
uv run seat-notifier --auto-login poll
```

Explicit `--term` and `--course` arguments remain available and override these environment values.

Check once:

```bash
uv run seat-notifier --auto-login poll \
  --term 202609 \
  --subject COMP \
  --course 350 \
  --once
```

Poll every 60 seconds (the default):

```bash
uv run seat-notifier \
  --auto-login \
  --webdriver-cookies "$XDG_RUNTIME_DIR/minerva-cookies.json" \
  poll --term 202609 --subject COMP --course 350
```

Repeat `--course` to poll multiple courses. For different subjects, use `SUBJECT:NUMBER`:

```bash
uv run seat-notifier --auto-login poll \
  --term 202609 \
  --course COMP:350 \
  --course MATH:240 \
  --course ECSE:205
```

Courses sharing a subject can use the shorter form:

```bash
uv run seat-notifier --auto-login poll \
  --term 202609 --subject COMP \
  --course 350 --course 551
```

All courses share one polling interval and authenticated session, while maintaining independent previous-state and notification-delivery tracking. Duplicate course specifications are ignored.

Set a different interval with `--interval SECONDS`. The command prints the initial Lecture-section state, then only reports increases in relevant availability compared with the immediately preceding poll. Capacity decreases, room/instructor changes, and other metadata changes do not produce an availability report. Logs still record checks with no opening. Stop continuous polling with Ctrl-C.

Only rows whose Banner type is exactly `Lecture` are tracked; exams, labs, tutorials, notes, spacer rows, and secondary meeting rows are ignored. If `WL Act` is greater than zero, the poller tracks `WL Rem` and reports only when that count increases (`WAITLIST SPOT OPENED`). Otherwise, it tracks regular `Rem` and reports when that count increases (`SEAT OPENED`). The initial display uses `WAITLIST OPEN`/`WAITLIST FULL` when a waitlist queue exists and `OPEN`/`CLOSED` otherwise.

Output includes CRN, section, capacity, remaining seats, waitlist capacity/remaining, title, meeting time, and location.

### Automatic waitlist submission

This feature changes your Minerva registration and is disabled by default. Enable it explicitly while polling:

```bash
uv run seat-notifier --auto-login poll \
  --term 202609 \
  --course COMP:350 \
  --auto-waitlist
```

When a tracked Lecture has positive `WL Rem` on the initial check, or `WL Rem` increases later, the client:

1. loads Minerva's Quick Add form, selecting the requested registration term first when a fresh session returns `Select Term`;
2. preserves every existing-schedule and worksheet field;
3. submits the CRN with Banner's `RW` Quick Add action;
4. if Banner offers `LW` (Add to Waitlist), submits the returned form with that action; and
5. verifies the CRN appears in Current Schedule with a waitlist status.

Test a specific CRN manually with an explicit confirmation flag:

```bash
uv run seat-notifier --auto-login waitlist-add \
  --term 202609 --crn 2359 --yes
```

Possible results include `added-to-waitlist`, `already-on-waitlist`, `waitlist-full`, `registered-directly`, and `rejected`. A full or rejected waitlist exits unsuccessfully. Network failures during automatic mode remain pending for retry, while a `waitlist-full` response waits for a future newly detected opening.

After `added-to-waitlist`, `already-on-waitlist`, or `registered-directly`, the poller sends a separate success notification and removes that entire course from the active polling set. Other watched courses continue normally; when none remain, the process exits without waiting for another interval.

**Important:** Banner's required first step is a regular Quick Add (`RW`). If a regular seat becomes available before that request is processed, Banner may register the CRN directly instead of offering the waitlist. The client reports this as `registered-directly`. Verify prerequisites, restrictions, schedule conflicts, credit limits, multi-term rules, and institutional policy before enabling this option.

### ntfy and D-Bus notifications

Each opening is sent independently to ntfy and the local desktop's `org.freedesktop.Notifications` D-Bus service. Local notifications use `notify-send`, which must be installed and able to access the graphical session bus.

Set an unguessable ntfy topic in the root `.env`:

```dotenv
NTFY_TOPIC=your-private-random-topic
# Optional for a self-hosted server or protected topic:
# NTFY_SERVER=https://ntfy.example.com
# NTFY_TOKEN=tk_example
```

Test both notification channels without loading Minerva cookies or launching Firefox:

```bash
uv run seat-notifier notify-test
```

Use `--no-dbus` to test only ntfy, or `--no-ntfy` to test only the desktop notification. The ntfy test uses the same topic, token, priority, and Minerva click destination as real alerts.

Each newly detected opening sends a high-priority notification containing the course, section, CRN, old availability, and new availability. Clicking the ntfy notification opens Minerva's registration menu. Override that destination with `MINERVA_REGISTRATION_URL`; for safety, click targets are restricted to HTTPS on `horizon.mcgill.ca`.

Delivery and retries are tracked separately per channel, so a D-Bus failure does not duplicate a successful ntfy alert. Failed deliveries remain pending and retry on subsequent polling cycles. Disable channels with `--no-ntfy` and/or `--no-dbus`.

The client posts the same ordered form fields as Minerva's course search, including Banner's duplicate subject, instructor, attribute, and level fields. Term IDs are six-digit Banner values such as `202609`, not human-readable semester names.

## Docker and Railway

The image includes Firefox ESR, Geckodriver, and the Python application. Its default command runs headless automatic login, polling, and ntfy notifications; D-Bus is disabled because Railway has no desktop session. Set `MINERVA_AUTO_WAITLIST=true` to explicitly enable registration-changing automatic waitlist submission.

Build and run locally:

```bash
docker build -t seat-notifier .
docker run --rm --env-file .env -p 8080:8080 seat-notifier
```

For Railway, deploy the repository using the included `Dockerfile` and `railway.toml`, then configure these service variables:

```text
MINERVA_USERNAME
MINERVA_PASSWORD
MINERVA_TOTP_SECRET
MINERVA_TERM
MINERVA_COURSES
NTFY_TOPIC
```

To opt in to automatic waitlisting, also set:

```text
MINERVA_AUTO_WAITLIST=true
```

Optionally set `NTFY_SERVER`, `NTFY_TOKEN`, or `MINERVA_REGISTRATION_URL`. Do not set `PORT`; Railway supplies it. No `.env` or browser cookies are copied into the image.

### Health server

Poll mode starts a threaded HTTP server on `0.0.0.0:$PORT` (`8080` outside Railway):

```text
GET /health
```

The first successful health request per process sends one test notification through each enabled channel. Later health probes return `already-sent` without sending duplicates. A failed test notification returns HTTP `503` and is retried by the next probe. Disable the server with `--no-health-server`, or override local binding with `--health-host` and `--health-port`/`HEALTH_PORT`.

Example responses:

```json
{"status":"ok","notification":"sent"}
{"status":"ok","notification":"already-sent"}
```
