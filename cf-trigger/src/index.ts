export interface Env {
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  DISPATCH_EVENT_TYPE: string;
}

export default {
  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    const res = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Accept': 'application/vnd.github+json',
          'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'cf-worker-midnight-booker',
        },
        body: JSON.stringify({ event_type: env.DISPATCH_EVENT_TYPE }),
      },
    );
    if (!res.ok) {
      throw new Error(`GitHub dispatch failed: ${res.status} ${await res.text()}`);
    }
  },
};
