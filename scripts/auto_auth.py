#!/usr/bin/env python3
"""Automated Kite Connect authentication using pyotp.

Logs into Kite Connect programmatically, generates TOTP,
and saves the access token to .env and .kite_access_token.

Usage:
    uv run python scripts/auto_auth.py

Required env vars:
    KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
"""

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from kiteconnect import KiteConnect

# Load .env manually (don't depend on dotenv)
ENV_FILE = Path(__file__).parent.parent / ".env"


def load_env():
    """Load .env file into os.environ (overwrites empty values)."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if value:  # Only set if .env has a non-empty value
                    os.environ[key] = value


def get_request_token(api_key: str, user_id: str, password: str, totp_secret: str) -> str:
    """Login to Kite and get the request_token.

    Flow:
        1. POST /api/login with user_id + password -> request_id
        2. Generate TOTP from secret
        3. POST /api/twofa with request_id + totp -> redirect with request_token
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "X-Kite-Version": "3",
    })

    # Step 1: Login with credentials
    print(f"  Logging in as {user_id}...")
    login_url = "https://kite.zerodha.com/api/login"
    login_resp = session.post(login_url, data={
        "user_id": user_id,
        "password": password,
    })

    if login_resp.status_code != 200:
        raise RuntimeError(f"Login failed: HTTP {login_resp.status_code} — {login_resp.text[:200]}")

    login_data = login_resp.json()
    if login_data.get("status") != "success":
        raise RuntimeError(f"Login failed: {login_data.get('message', login_data)}")

    request_id = login_data["data"]["request_id"]
    print(f"  Login OK, request_id={request_id[:8]}...")

    # Step 2: Generate TOTP
    totp = pyotp.TOTP(totp_secret)
    otp = totp.now()
    print(f"  TOTP generated: {otp}")

    # Step 3: Submit 2FA (try multiple API formats — Zerodha changes these)
    twofa_url = "https://kite.zerodha.com/api/twofa"
    twofa_payloads = [
        # Format 1: minimal (works as of Mar 2026)
        {"user_id": user_id, "request_id": request_id, "twofa_value": otp},
        # Format 2: explicit totp type
        {"user_id": user_id, "request_id": request_id, "twofa_value": otp, "twofa_type": "totp"},
        # Format 3: app_code type
        {"user_id": user_id, "request_id": request_id, "twofa_value": otp, "twofa_type": "app_code"},
    ]

    twofa_resp = None
    for i, payload in enumerate(twofa_payloads):
        twofa_resp = session.post(twofa_url, data=payload)
        if twofa_resp.status_code == 200:
            break
        print(f"  2FA format {i+1} failed: {twofa_resp.json().get('message', '')}")

    if twofa_resp.status_code != 200:
        # TOTP might have just expired — wait and retry with next code
        print("  Waiting for next TOTP code...")
        time.sleep(max(1, 30 - time.time() % 30 + 1))
        otp = totp.now()
        print(f"  Retrying with TOTP: {otp}")
        for payload in twofa_payloads:
            payload["twofa_value"] = otp
            twofa_resp = session.post(twofa_url, data=payload)
            if twofa_resp.status_code == 200:
                break

    if twofa_resp.status_code != 200:
        raise RuntimeError(f"2FA failed: HTTP {twofa_resp.status_code} — {twofa_resp.text[:200]}")

    twofa_data = twofa_resp.json()
    if twofa_data.get("status") != "success":
        raise RuntimeError(f"2FA failed: {twofa_data.get('message', twofa_data)}")

    print("  2FA OK")

    # Step 4: Get request_token by following the redirect chain
    # After 2FA, hit connect/login → Kite redirects through /connect/finish → callback with request_token
    urls_to_try = [
        f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3",
    ]

    for url in urls_to_try:
        resp = session.get(url, allow_redirects=False)

        # Follow redirect chain (max 5 hops)
        for _ in range(5):
            if resp.status_code not in (301, 302, 303):
                break
            location = resp.headers.get("Location", "")

            # Check if this redirect contains request_token
            parsed = urlparse(location)
            params = parse_qs(parsed.query)
            request_token = params.get("request_token", [None])[0]
            if request_token:
                print(f"  Got request_token: {request_token[:12]}...")
                return request_token

            # Follow the redirect
            if location.startswith("/"):
                location = f"https://kite.zerodha.com{location}"
            resp = session.get(location, allow_redirects=False)

        # Check response body for request_token
        if resp.status_code == 200:
            match = re.search(r'request_token=([a-zA-Z0-9]+)', resp.text)
            if match:
                print(f"  Got request_token from page: {match.group(1)[:12]}...")
                return match.group(1)

    raise RuntimeError(
        f"Could not extract request_token. "
        f"Last status={resp.status_code}, "
        f"Last location={resp.headers.get('Location', 'none')}"
    )


def exchange_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Exchange request_token for access_token using KiteConnect API."""
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token=request_token, api_secret=api_secret)
    return data["access_token"]


def save_token(access_token: str):
    """Save access_token to .env and .kite_access_token."""
    # Save to .kite_access_token
    token_file = Path(__file__).parent.parent / ".kite_access_token"
    token_file.write_text(access_token)
    token_file.chmod(0o600)

    # Update .env
    env_content = ENV_FILE.read_text()
    env_content = re.sub(
        r"KITE_ACCESS_TOKEN=.*",
        f"KITE_ACCESS_TOKEN={access_token}",
        env_content,
    )
    ENV_FILE.write_text(env_content)
    print(f"  Token saved to .env and .kite_access_token")


def verify_token(api_key: str, access_token: str) -> bool:
    """Verify the token works by making a profile API call."""
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        profile = kite.profile()
        print(f"  Verified: logged in as {profile['user_name']} ({profile['user_id']})")
        return True
    except Exception as e:
        print(f"  Verification failed: {e}")
        return False


def main():
    load_env()

    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")
    user_id = os.environ.get("KITE_USER_ID", "")
    password = os.environ.get("KITE_PASSWORD", "")
    totp_secret = os.environ.get("KITE_TOTP_SECRET", "")

    missing = []
    if not api_key:
        missing.append("KITE_API_KEY")
    if not api_secret:
        missing.append("KITE_API_SECRET")
    if not user_id:
        missing.append("KITE_USER_ID")
    if not password:
        missing.append("KITE_PASSWORD")
    if not totp_secret:
        missing.append("KITE_TOTP_SECRET")

    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        print(f"Set them in {ENV_FILE}")
        sys.exit(1)

    print("=" * 50)
    print("  Kite Connect Auto-Auth")
    print("=" * 50)

    try:
        # Step 1: Login and get request_token
        request_token = get_request_token(api_key, user_id, password, totp_secret)

        # Step 2: Exchange for access_token
        print("  Exchanging for access token...")
        access_token = exchange_token(api_key, api_secret, request_token)
        print(f"  Access token: {access_token[:12]}...")

        # Step 3: Save
        save_token(access_token)

        # Step 4: Verify
        verify_token(api_key, access_token)

        print("=" * 50)
        print("  Auth complete! System is ready to start.")
        print("  Run: uv run python -m src.main")
        print("=" * 50)

    except Exception as e:
        print(f"\n  AUTH FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
