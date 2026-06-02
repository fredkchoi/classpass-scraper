# cf-trigger

A tiny Cloudflare Worker that fires a GitHub `repository_dispatch` event on a cron schedule. Part of [classpass-scraper](../README.md). Replaces GitHub Actions' built-in `schedule:` cron, which can be delayed 25 minutes to several hours on small repos (fatal for a job that must fire at midnight ET when the ClassPass 7-day booking window opens).

## How it works

Cloudflare cron fires at `0 3 * * *` (03:00 UTC = 11:00pm EDT / 10:00pm EST), the Worker `POST`s to `https://api.github.com/repos/<owner>/<repo>/dispatches` with `{"event_type": "midnight-booker"}`, then the `midnight-booker.yml` workflow (configured with `on: repository_dispatch`) runs. The Python `scheduler.py` script wakes up, computes each target's exact venue-local window-open time, and sleeps until then before booking.

## Setup

1. **Create a fine-grained GitHub PAT** at https://github.com/settings/personal-access-tokens/new
   - Repository access: only your `classpass-scraper` fork
   - Repository permissions: `Contents: Read and write`, `Metadata: Read-only`
   - (Note: `repository_dispatch` requires `Contents: write` despite the endpoint living under `/dispatches`. GitHub's docs are misleading on this.)
2. **Edit `wrangler.jsonc`** to set `GITHUB_REPO` to your fork's `owner/repo`.
3. **Store the PAT as a Worker secret**:
   ```bash
   npx wrangler secret put GITHUB_TOKEN
   ```
4. **Deploy**:
   ```bash
   npx wrangler deploy
   ```

## Verify

To manually fire the scheduled handler locally and confirm the GitHub dispatch works:

```bash
npx wrangler dev --test-scheduled
# In a second terminal:
curl "http://localhost:8787/__scheduled?cron=0+3+*+*+*"
# Then check the target repo:
gh run list --workflow=midnight-booker.yml --limit 3
```

You should see a new run with `event = repository_dispatch`. The production Worker fires automatically at 03:00 UTC nightly.

## Multi-timezone caveat

The Worker fires once at 03:00 UTC, which corresponds to 11:00pm Eastern (EDT) or 10:00pm Eastern (EST). For non-Eastern venues, the workflow's `timeout-minutes: 150` gives enough headroom to sleep until that venue's local midnight. If you target venues in timezones west of Eastern that need a different fire time, add additional cron expressions to `wrangler.jsonc` or run multiple Workers.
