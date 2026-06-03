import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ValueError(f"Missing required env var: {key}")
    return val


SENDER_EMAIL = _require("SENDER_EMAIL")
SENDER_PASSWORD = _require("SENDER_PASSWORD")
RECIPIENT_EMAIL = _require("RECIPIENT_EMAIL")

# ClassPass auth, token is required for booking. The token is captured manually from
# DevTools (see README "Capturing your ClassPass auth token") and is long-lived. There
# is no auto-refresh; scheduler/cancellation_poller email if the token goes stale.
CLASSPASS_AUTH_TOKEN = os.getenv("CLASSPASS_AUTH_TOKEN", "")
CLASSPASS_USER_ID = os.getenv("CLASSPASS_USER_ID", "")

POLL_INTERVAL_SECONDS = 30 * 60
