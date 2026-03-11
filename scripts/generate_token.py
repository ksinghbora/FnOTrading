"""Generate Kite Connect access token via browser login flow.

Usage:
    python scripts/generate_token.py

This will:
1. Open the Kite login page in your browser
2. After login, Kite redirects to a URL containing the request_token
3. Paste the full redirect URL when prompted
4. The script exchanges it for an access token and saves it
"""

import re
import sys
import webbrowser
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Settings


def main():
    settings = Settings()

    if not settings.kite_api_key or not settings.kite_api_secret:
        print("ERROR: Set KITE_API_KEY and KITE_API_SECRET in .env file first")
        sys.exit(1)

    from src.broker.zerodha.auth import KiteAuth

    auth = KiteAuth(settings.kite_api_key, settings.kite_api_secret)

    # Open login URL
    login_url = auth.login_url
    print(f"\nOpening Kite login page...\n{login_url}\n")
    webbrowser.open(login_url)

    # Wait for redirect URL
    print("After logging in, you'll be redirected to a URL like:")
    print("  https://127.0.0.1/?request_token=XXXXX&action=login&status=success")
    print()
    redirect_url = input("Paste the full redirect URL here: ").strip()

    # Extract request token
    match = re.search(r"request_token=([a-zA-Z0-9]+)", redirect_url)
    if not match:
        print("ERROR: Could not find request_token in URL")
        sys.exit(1)

    request_token = match.group(1)
    print(f"\nRequest token: {request_token}")

    # Generate access token
    access_token = auth.generate_session(request_token)
    print(f"Access token: {access_token}")
    print(f"\nToken saved to .kite_access_token")
    print(f"\nUpdate your .env file:")
    print(f"  KITE_ACCESS_TOKEN={access_token}")


if __name__ == "__main__":
    main()
