# cf-trigger

A tiny Cloudflare Worker that fires a GitHub `repository_dispatch` event on a cron schedule. Part of [classpass-scraper](../README.md). Replaces GitHub Actions' built-in `schedule:` cron, which can be delayed 25 minutes to several hours on small repos (fatal for a booker that must fire at the exact moment the ClassPass booking window opens).

## How it works

Cloudflare cron fires every hour (`0 * * * *` UTC), the Worker `POST`s to `https://api.github.com/repos/<owner>/<repo>/dispatches` with `{"event_type": "hourly-booker"}`, then the `hourly-booker.yml` workflow (configured with `on: repository_dispatch`) runs. The Python `scheduler.py` script queries the ClassPass schedule API for each target, parses the release moment, and either books now, sleeps until the exact release moment within the hour, or skips (next hourly run picks it up).

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
curl "http://localhost:8787/__scheduled?cron=0+*+*+*+*"
# Then check the target repo:
gh run list --workflow=hourly-booker.yml --limit 3
```

You should see a new run with `event = repository_dispatch`. The production Worker fires automatically every hour.
