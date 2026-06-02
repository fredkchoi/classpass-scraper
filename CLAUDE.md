# CLAUDE.md

## General Rules

- **Always update README.md** when making any change that affects features, configuration, environment variables, workflows, scheduling, or usage. Do not wait to be asked.
- **Always update this CLAUDE.md** when making changes that affect the scheduling architecture, booking flow, or key file inventory.
- **Never use em dashes** in responses or any frontend text. Use commas, parentheses, or rewrite the sentence instead.

## Project Overview

Python automation for booking ClassPass sessions across multiple venues. Runs entirely in the cloud, no local machine needed after setup. Sibling to and modeled after `../five-iron-scraper`, but adapted for:
- multiple venues per repo (venue_id stored per-target in `targets.json`)
- venue-local timezones (booking window opens at midnight venue-local, not always ET)
- ClassPass dynamic credit pricing (the cost is re-fetched right before booking)

The booking job is triggered by a Cloudflare Worker cron (more reliable than GHA's native scheduler) which fires a GitHub `repository_dispatch` event; GitHub Actions does the actual Python work. A GHA `schedule:` cron at the same time remains as a backup.

## Key Files

| File | Purpose |
|---|---|
| `scheduler.py` | Main nightly runner. Computes each target's window-open time in venue-local tz, waits, books |
| `book.py` | Booking logic: `POST /_api/v1/users/{user_id}/reservations` with `{schedule, credits}` + retry-on-401 |
| `auth.py` | Email/password login to refresh `CLASSPASS_AUTH_TOKEN`. **Login endpoint is currently a stub**, needs the actual login request captured from DevTools to be wired in |
| `availability.py` | `POST /_api/v3/search/schedules` via `curl_cffi` (impersonates Chrome's TLS handshake to defeat Cloudflare bot detection) |
| `cancellation_poller.py` | Hourly job that watches polling targets. Auto-books via `attempt_booking` when a matching slot opens; emails if no token is configured or booking fails |
| `gcal.py` | Google Calendar event creation after a booking |
| `monday_prompt.py` | Weekly Monday email summarizing upcoming booking windows |
| `targets.json` | What to book. Schema: `venue_id`, `date`, `time?`, `class_name_contains?`, `teacher_contains?`, `max_credits?`, `lead_days?` |
| `validate_targets.py` | Schema linter for `targets.json` (run in CI on every push) |
| `config.py` | Env var loading |
| `notifier.py` | Email notifications via Gmail SMTP |
| `cf-trigger/` | Cloudflare Worker that fires `repository_dispatch` on cron, primary trigger for `midnight-booker.yml` |

## Booking Flow

1. `POST /_api/v3/search/schedules` with `{date, venue: [<id>], ...}` returns class list with `schedule.id` and `availability.credits`
2. `POST /_api/v1/users/{CLASSPASS_USER_ID}/reservations` with `{"schedule": <id>, "credits": <credits>}` and `cp-authorization: Token <CLASSPASS_AUTH_TOKEN>`

**Never hardcode credits**, they're dynamic. Always read `availability.credits` from a fresh search response and pass it into the reservation.

**ClassPass is behind Cloudflare**. Use `curl_cffi` with `impersonate="chrome"` for any request that hits classpass.com, plain `requests` gets a 403 "Just a moment..." challenge.

## Auth

`cp-authorization` token is captured from the user's browser DevTools and stored as `CLASSPASS_AUTH_TOKEN`. It is long-lived (weeks to months).

On a 401, `book.py` calls `auth.refresh_token()` which logs in with `CLASSPASS_EMAIL` / `CLASSPASS_PASSWORD`. The exact login endpoint is `https://classpass.com/api/v2/auth/login` but the request body shape and response token field have not been captured (Chrome DevTools' service worker integration prevented capturing the body). `auth.py` has TODOs marking what needs to be confirmed.

In GitHub Actions, an in-memory refresh does not persist across runs. If the token frequently expires, we'd need to push the rotated token back to GitHub Secrets (requires a PAT with `secrets:write`).

## Booking Window

ClassPass venues typically advertise "Opens 7 Days in Advance" (visible in `venue.booking_window` in the search response). The scheduler:
- Defaults to `lead_days = 7`
- Computes window-open time as venue-local midnight `lead_days` before the class date
- Uses `venue.tz` from the search response to pick the timezone
- Per-target `lead_days` override is supported

Whether ClassPass actually releases at midnight venue-local is empirical. If not, the scheduler's booking attempt fails, the target is flagged `"status": "polling"`, and `cancellation_poller.py` keeps trying hourly until it succeeds.

## Scheduling

- **Cloudflare Worker** (primary): fires at `0 3 * * *` UTC (= 11pm EDT / 10pm EST) and POSTs `repository_dispatch` with `event_type: midnight-booker` to GitHub.
- **GHA `schedule:` cron** (backup): same `0 3 * * *` slot, kept in case the CF Worker fails.
- **Workflow `timeout-minutes: 150`** to accommodate EST winter case where the workflow fires at 10pm EST and sleeps ~2hr until venue-local midnight.
- **Booking semantics (`scheduler.py`)**:
  - Excludes polling targets (those are owned by `cancellation_poller.py`)
  - Targets whose window opens in the next 6 hours: wait until exact open time, then book
  - Targets already open (window already passed): book immediately
  - Failed bookings: written back to `targets.json` with `"status": "polling"`

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `midnight-booker.yml` | `repository_dispatch` (from CF Worker) + `schedule` (backup) + `workflow_dispatch` | Nightly booking attempt |
| `cancellation-poller.yml` | `schedule` hourly | Watches polling targets; auto-books matching slots, email fallback if booking fails |
| `monday-prompt.yml` | `schedule` Monday 9am ET | Weekly email reminder to confirm targets |
| `validate-targets.yml` | Push/PR touching `targets.json` | Lints schema; blocks merge on failure |
