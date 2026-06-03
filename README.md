# ClassPass Scraper

Automation for booking ClassPass sessions across multiple venues. Mirrors the layout of [five-iron-scraper](../five-iron-scraper) but adapted for ClassPass's multi-venue booking model and dynamic credit pricing.

ClassPass releases bookings ~7 days before the class. Popular classes (e.g. [solidcore] reformer pilates) sell out fast. This tool watches your target classes, fires when the booking window opens, and reserves your spot.

## Features

- **Availability notifier** (`main.py`) — polls a venue/date on demand and emails when matching classes are available
- **Auto-booker** (`scheduler.py`) — books at the moment the 7-day window opens (venue-local midnight), with retries
- **Cancellation poller** (`cancellation_poller.py`) — if initial booking fails, watches hourly and auto-books when spots open (falls back to email if no auth token is configured)
- **Weekly prompt** (`monday_prompt.py`) — emails you each Monday with upcoming booking windows
- **Google Calendar** — optional, creates an event after each successful booking

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
| `CLASSPASS_AUTH_TOKEN` | Auth token captured from DevTools (see below) |
| `CLASSPASS_USER_ID` | Your ClassPass user ID (see below) |
| `CLASSPASS_EMAIL` | (Optional) Account email for auto-refresh of the token |
| `CLASSPASS_PASSWORD` | (Optional) Account password for auto-refresh |
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

If the token expires, the booker will email you and (if `CLASSPASS_EMAIL` + `CLASSPASS_PASSWORD` are set and the login endpoint in `auth.py` is wired up) attempt to refresh automatically.

### Finding a venue ID

1. Open a venue page at classpass.com (e.g. `https://classpass.com/studios/solidcore-long-island-city`)
2. Open DevTools → **Network** tab → filter by `_api/v3/search/schedules`
3. Check the request payload. The `venue` array contains the numeric venue ID

## How booking works

1. **Search** — `POST /_api/v3/search/schedules` returns the list of classes at a venue for a given date, each with a `schedule.id` and dynamic `availability.credits` cost. ClassPass gates this endpoint behind Cloudflare bot detection, so the request needs a real user-agent, `platform: web`, and the `cp-authorization` token.
2. **Balance check** — `GET /_api/v3/lifecycle/user/{user_id}/balance` returns `{"data": {"credit_balance": {"credits_remaining": N}}}`. The scheduler fetches this at the start of each target and skips preferences whose dynamic credit cost exceeds the balance (no point sleeping until release or POSTing a reservation that will fail).
3. **Reserve** — `POST /_api/v1/users/{user_id}/reservations` with `{"schedule": <id>, "credits": <credits>}` and the `cp-authorization` token

The booker re-fetches the search response right before reserving so it sends the current credit cost (ClassPass uses dynamic pricing). If the balance fetch itself fails the booker falls back to the previous behavior of attempting the reservation anyway.

## targets.json

Pre-populate this with everything you want to book — the scheduler picks up entries whose 7-day window opens that night.

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

Each target represents a single intention (e.g. "get me into a class on June 10 evening"). Inside it, a `preferences` array lists priority-ordered alternatives. The scheduler tries each in order at the release moment and the first available preference wins. The confirmation email lists every preference and notes which one was actually booked.

Top-level fields are inherited by every preference; per-preference fields override. So if every preference shares the same venue, date, and class name, you only write those once.

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
| `status` | target | auto | Set to `"polling"` by the booker when no preference can be booked |

Backward-compatible: if a target has no `preferences` array, its top-level fields are treated as a single preference. After a successful booking the target is removed; if every preference is unavailable the whole target moves to polling.

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
python scheduler.py
# Validates targets.json
# For each target, queries the ClassPass schedule API and branches on what it sees:
#   status="available"            -> book immediately
#   reason="out_of_spots"         -> flag as polling for the cancellation watcher
#   reason="before_opening_window" -> parse release time from `credits_reasons`
#                                     (e.g. "The booking window opens on 6/3/26, 5:00 AM"),
#                                     sleep until exactly then if within 75 min,
#                                     otherwise skip (next hourly run picks it up)
```

## Cloud architecture

Everything runs in the cloud, your PC can stay off. Booking is triggered by a Cloudflare Worker on an hourly cron, because GitHub Actions' built-in `schedule:` cron can be delayed by hours on small repos (fatal when popular classes sell out in seconds at the exact release moment).

The CF Worker fires every hour; each fire runs a short GHA job that queries the schedule API and either acts now or waits until the precise release moment within that hour. No per-studio configuration is needed: the API itself tells us when each class opens.

The booker workflow uses GHA `concurrency:` so only one run is in flight at a time. If a run is sleeping until a release moment within the hour and the next hourly CF dispatch arrives, the new run queues until the sleeping run commits + finishes, then starts against the post-booking state and exits as a no-op. Prevents the in-flight run from racing the queued run on the reservation endpoint.

| Component | Trigger | What it does |
|---|---|---|
| `cf-trigger/` (Cloudflare Worker) | CF cron at `0 * * * *` (hourly) | Sends `repository_dispatch` to the GitHub repo |
| `midnight-booker.yml` (GHA) | `repository_dispatch` + `schedule` hourly backup | Runs `scheduler.py`, which queries the API and books (or waits + books) |
| `cancellation-poller.yml` (GHA) | Hourly | On polling targets: auto-books when matching classes open up (or emails if no auth token is set) |
| `monday-prompt.yml` (GHA) | Every Monday ~9am ET | Emails upcoming booking windows for the next 2 weeks |
| `validate-targets.yml` (GHA) | On `targets.json` push/PR | Lints the file, blocks merge if invalid |

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
- No duplicate targets (same venue + date + time + class filter)
- No unknown fields
