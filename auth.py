"""
ClassPass auth-token refresh from email + password.

Endpoint confirmed: POST https://classpass.com/api/v2/auth/login

STILL TO CONFIRM:
  - The request body shape (Payload tab in DevTools — ~120 bytes of JSON)
  - Where the token lives in the response (Response/Preview tab in DevTools).
    The token is the "Token <hex>" value used in `cp-authorization` for subsequent
    requests. It probably lives at one of: data["token"], data["auth_token"],
    data["user"]["token"], or similar.

Update `payload` and the token extraction below once both shapes are captured.

Until login is fully wired, set CLASSPASS_AUTH_TOKEN manually in .env / GitHub secrets.
The booker will email you on 401 if it can't refresh.
"""

from curl_cffi import requests
from config import CLASSPASS_EMAIL, CLASSPASS_PASSWORD

LOGIN_URL = "https://classpass.com/api/v2/auth/login"


def login(email: str = "", password: str = "") -> str:
    """
    Exchange email + password for a `cp-authorization` token.
    Returns the token string or '' on failure.

    Update the `payload` shape + response parsing to match the captured request.
    """
    email = email or CLASSPASS_EMAIL
    password = password or CLASSPASS_PASSWORD
    if not email or not password:
        print("[auth] CLASSPASS_EMAIL / CLASSPASS_PASSWORD not set — cannot refresh token")
        return ""

    # TODO: replace with the captured request body. Common shapes are:
    #   {"email": "...", "password": "..."}
    #   {"user": {"email": "...", "password": "..."}}
    payload = {"email": email, "password": password}
    headers = {
        "content-type": "application/json",
        "platform": "web",
        "accept-language": "en-US",
        "user-agent": "Mozilla/5.0",
    }

    try:
        resp = requests.post(LOGIN_URL, json=payload, headers=headers, timeout=15, impersonate="chrome")
        if not resp.ok:
            print(f"[auth] login HTTP {resp.status_code}: {resp.text[:300]}")
            return ""
        data = resp.json()
    except Exception as e:
        print(f"[auth] login failed: {e}")
        return ""

    # TODO: confirm where the token lives in the response. Try common fields first.
    token = (
        data.get("token")
        or data.get("auth_token")
        or data.get("user", {}).get("token")
        or ""
    )
    if not token:
        print(f"[auth] login response did not contain a token. Keys: {list(data.keys())}")
    return token


def refresh_token() -> str:
    """Convenience: re-login with stored credentials and return the new token."""
    return login()
