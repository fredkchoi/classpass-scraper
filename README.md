# ClassPass Scraper

Books popular ClassPass classes at the exact moment their reservation window opens. A Cloudflare Worker fires once an hour, the GitHub Actions job parses each target's release time from the ClassPass API, sleeps inside the hour until that exact moment, and fires a tight reservation loop. Anything that can't be booked at release moves to a polling state and the next hourly run keeps watching for cancellations.

Built for popular venues like [solidcore] reformer pilates, where spots disappear within seconds of the window opening.

## Features

- **Auto-booker** (`scheduler.py`) — parses the release moment from each target's API response (the window can be 5 AM ET, midnight venue-local, or anything else — we read it from the API per-target, no hardcoded rules) and books at that exact second using a 150ms reservation retry loop
- **Cancellation poller** (`cancellation_poller.py`) — runs hourly alongside the scheduler; auto-books any polling target whose class has a spot open this hour
- **Priority-ordered preferences** — a target can list multiple time/teacher alternatives; the bot tries them in priority order and the first that succeeds wins
- **Email notifications** — booking confirmations, "couldn't book" alerts when a target moves to polling, and "rotate your auth token" alerts when auth goes stale
- **Google Calendar** — optional event creation after a successful booking
- **Reliable cron** — Cloudflare Worker dispatch instead of GHA's `schedule:` cron, which can be delayed 25 min+ on small repos (fatal at a release window)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your .env values
```

## Configuration

| Variable | Description |
|---|---|
| `SENDER_EMAIL` | Gmail address to send notifications from |
| `SENDER_PASSWORD` | Gmail [App Password](https://myaccount.google.com/apppasswords) |
| `RECIPIENT_EMAIL` | Email address to receive notifications |
| `CLASSPASS_AUTH_TOKEN` | Auth token captured from DevTools (see below). Rotate manually when the bot emails you about a stale token |
| `CLASSPASS_USER_ID` | Your ClassPass user ID (see below) |
| `GOOGLE_CLIENT_ID` | (Optional) Google OAuth2 client ID for Calendar |
| `GOOGLE_CLIENT_SECRET` | (Optional) |
| `GOOGLE_REFRESH_TOKEN` | (Optional) Obtained once via the gcal setup flow |
| `GOOGLE_CALENDAR_ID` | (Optional) Target calendar ID (default: `primary`) |
| `GOOGLE_EVENT_COLOR` | (Optional) Event color (default: `peacock`) |

### Capturing your ClassPass auth token and user ID

ClassPass uses a Django-REST-Framework-style token in a `cp-authorization: Token <hex>` header. The token is long-lived (typically weeks to months) and reuses your existing session.

1. Sign in at [classpass.com](https://classpass.com)
2. Open DevTools (F12) → **Network** tab
3. Click around (e.g. browse any studio schedule) to trigger an `_api/` request
4. Inspect the request headers. Copy the value after `Token ` from `cp-authorization` into `CLASSPASS_AUTH_TOKEN`
5. The `_api/v1/users/<USER_ID>/...` path contains your numeric `CLASSPASS_USER_ID`

When the token eventually goes stale, the next hourly run will detect it (the credit-balance pre-check returns `None`) and email you with these same steps. Repeat them and update the `CLASSPASS_AUTH_TOKEN` GitHub Secret. There is no auto-refresh: ClassPass's login endpoint is gated behind a service worker we can't capture without mitmproxy in front of the browser, which isn't worth the setup for a token that rotates this rarely.

### Finding a venue ID

1. Open a venue page at classpass.com (e.g. `https://classpass.com/studios/solidcore-long-island-city`)
2. Open DevTools → **Network** tab → filter by `_api/v3/search/schedules`
3. Check the request payload. The `venue` array contains the numeric venue ID

## How booking works

Every hour, the scheduler does:

1. **Balance check** — `GET /_api/v3/lifecycle/user/{user_id}/balance` returns remaining credits. Also serves as an auth health check: a `None` return triggers the "rotate your token" email.

2. **Per-preference schedule lookup** — `POST /_api/v3/search/schedules` for each preference on each target. The response tells us one of:
   - `status=available` → book immediately (slow path, search + reserve per attempt)
   - `reason=out_of_spots` → sold out; flag target as polling and email you
   - `reason=before_opening_window` → parse release time from `credits_reasons` (e.g. "The booking window opens on 6/4/26, 5:00 AM")

3. **Wait for release** — pick the earliest affordable release moment across all of a target's preferences. If within 75 min, sleep until that exact moment in-process. Otherwise skip; the next hourly run picks it up.

4. **Tight reservation loop** — at the release moment, fire `POST /_api/v1/users/{user_id}/reservations` directly using the schedule_id we already cached from the pre-window search. Retries every 150ms (popular classes sell out in well under a second, so we cut the per-attempt search round-trip from earlier rounds). Each preference gets up to 5 min, abandoning early on a confirmed `out_of_spots` response. First successful reservation wins.

5. **Polling fallback** — if no preference could be booked, the whole target moves to `status: polling` and you get a "couldn't book" email. The `cancellation_poller.py` portion of subsequent hourly runs keeps watching until either a spot opens up or the class date passes.

ClassPass gates these endpoints behind Cloudflare bot detection, so requests use `curl_cffi` with `impersonate="chrome"` to clear the TLS fingerprint check. Credit cost is dynamic (your credit price differs from retail), so we always read it from a fresh response, with `retail_price_in_credits` as a fallback if the API hasn't populated the personalized cost yet.

## targets.json

Pre-populate this with everything you want to book. The hourly scheduler picks up any target whose release moment falls inside the current hour and books at that exact second.

```json
{
  "targets": [
    {
      "label": "June 10 evening solidcore LIC",
      "venue_id": 235412,
      "date": "2026-06-10",
      "class_name_contains": "Signature50",
      "preferences": [
        {"time": "20:15"},
        {"time": "19:15"},
        {"time": "21:15"}
      ]
    }
  ]
}
```

Each target is one intention ("get me into a class on June 10 evening"). The `preferences` array lists priority-ordered alternatives — the bot tries them in order at the release moment and the first one that succeeds wins. The confirmation email lists every preference and notes which one was booked.

Top-level fields are inherited by every preference; per-preference fields override. So if every preference shares the same venue, date, and class name, you only write those once. Any field in the table below works at either level, so a preference can override the venue, date, class name filter, max credits, etc. For example, to mostly look for `Signature50` but accept `Foundation50` at the 10:15 PM slot as a last resort:

```json
{
  "class_name_contains": "Signature50",
  "preferences": [
    {"time": "19:15"},
    {"time": "20:15"},
    {"time": "22:15", "class_name_contains": "Foundation50"}
  ]
}
```

| Field | Level | Required | Description |
|---|---|---|---|
| `label` | target | no | Human-readable name, used in logs and emails |
| `venue_id` | either | yes | Numeric ClassPass venue ID |
| `date` | either | yes | `YYYY-MM-DD` |
| `time` | either | no | `HH:MM` (24hr, venue-local). Omit to match any time |
| `class_name_contains` | either | no | Case-insensitive substring match on the class name |
| `teacher_contains` | either | no | Case-insensitive substring match on the teacher name |
| `max_credits` | either | no | Refuse to book if the class costs more than this |
| `release_at` | either | no | ISO datetime override for when the booking window opens. Normally inferred from the API; set this only if the API message format changes or you want to test |
| `preferences` | target | no | Priority-ordered array of partial preference dicts. Omit to treat the target's top-level fields as a single preference |
| `status` | target | auto | Set to `"polling"` by the booker when no preference could be reserved |

If a target has no `preferences` array, its top-level fields are treated as a single preference. After a successful booking the target is removed from `targets.json`; if every preference is unavailable the whole target moves to polling.

## Usage

### Manual availability check
```bash
python main.py
# Prompts for venue_id, date, optional time, optional class name filter
# Polls every 30 minutes and emails when matching classes are available
```

### Add dates interactively
```bash
python monday_prompt.py
# Or just edit targets.json directly
```

### Hourly scheduler
```bash
python -u scheduler.py
# Reads targets.json. For each non-polling target, follows the decision
# tree in "How booking works" above.
```

### Cancellation poller
```bash
python -u cancellation_poller.py
# Reads targets.json. For each polling target, books immediately if a
# spot is available right now. In CI this runs before scheduler.py in
# the same workflow.
```

## Cloud architecture

Everything runs in the cloud; your machine can stay off.

| Component | Trigger | What it does |
|---|---|---|
| `cf-trigger/` (Cloudflare Worker) | CF cron `0 * * * *` (hourly) | POSTs `repository_dispatch` (event_type `hourly-booker`) to GitHub |
| `hourly-booker.yml` (GHA) | `repository_dispatch` (from Worker) + `schedule` hourly backup + `workflow_dispatch` | Runs `cancellation_poller.py` then `scheduler.py` in one job; commits + pushes any `targets.json` updates at the end |
| `monday-prompt.yml` (GHA) | Monday ~9am ET | Emails upcoming booking windows for the next 2 weeks |
| `validate-targets.yml` (GHA) | On `targets.json` push/PR | Lints the file; blocks merge if invalid |

The Cloudflare Worker exists because GHA's `schedule:` cron can be delayed 25 min to several hours on small repos, which would miss release windows entirely. Cloudflare crons fire within seconds of the target minute, so we use them as the primary trigger and keep the GHA cron as a backup.

GHA `concurrency: classpass-booker` serializes runs: if a run is sleeping until a release moment within the hour, the next hourly dispatch queues until that one finishes. This prevents two runs from racing on the reservation endpoint or stomping on each other's `targets.json` commits.

### Setup
1. Push this repo to GitHub (private recommended)
2. **Settings → Secrets and variables → Actions** — add every variable from `.env.example` as a repository secret
3. Enable Actions on the repo
4. Set up the Cloudflare Worker following [`cf-trigger/README.md`](cf-trigger/README.md). Requires a Cloudflare account (free tier is enough) and a fine-grained GitHub PAT.

### Lint `targets.json` locally
```bash
python validate_targets.py
```

Checks:
- Valid JSON and schema (`targets` array present)
- `venue_id` is an integer
- `date` is `YYYY-MM-DD` and in the future
- `time` (if set) is `HH:MM` 24hr
- `release_at` (if set) is a valid ISO datetime
- `max_credits` is a positive integer
- No unknown fields
