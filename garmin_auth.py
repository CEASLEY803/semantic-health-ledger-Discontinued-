"""
One-time Garmin authentication setup.

Usage:
    .venv\\Scripts\\python.exe garmin_auth.py

If you are rate-limited (429), see the instructions printed below.
Tokens are saved to data/garmin_tokens/garmin_tokens.json. Once saved,
future runs load from disk and never touch Garmin SSO.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
# Show all strategy attempts so we can see exactly what fails and why.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s  %(name)s  %(message)s",
)

email    = os.getenv("GARMIN_EMAIL", "").strip()
password = os.getenv("GARMIN_PASSWORD", "").strip()

if not email or not password:
    print("ERROR: Set GARMIN_EMAIL and GARMIN_PASSWORD in .env first.")
    raise SystemExit(1)

try:
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
    )
except ImportError:
    print("ERROR: Run:  .venv\\Scripts\\python.exe -m pip install garminconnect")
    raise SystemExit(1)

TOKEN_DIR  = Path(__file__).parent / "data" / "garmin_tokens"
TOKEN_FILE = TOKEN_DIR / "garmin_tokens.json"
TOKEN_DIR.mkdir(parents=True, exist_ok=True)

# ── Check for existing tokens first ──────────────────────────────────────────
if TOKEN_FILE.exists():
    print(f"\nExisting tokens found at {TOKEN_FILE}")
    print("Verifying they are still valid...\n")
    try:
        client = Garmin(email=email, password=password)
        client.login(tokenstore=str(TOKEN_DIR))
        info = client.get_full_name()
        print(f"✓ Tokens valid. Logged in as: {info}")
        print("Done. No re-authentication needed.")
        raise SystemExit(0)
    except Exception as e:
        print(f"Existing tokens invalid ({e}), re-authenticating...\n")
        TOKEN_FILE.unlink(missing_ok=True)

# ── Fresh authentication ──────────────────────────────────────────────────────
print("=" * 64)
print(f"Authenticating {email}")
print("=" * 64)
print()
print("  All login failures will be printed above.")
print("  garminconnect tries 5 strategies in order: mobile+cffi, mobile+requests,")
print("  widget+cffi, portal+cffi, and portal+requests.")
print()

# ── RATE LIMIT INSTRUCTIONS ───────────────────────────────────────────────────
print("  ╔══════════════════════════════════════════════════════════╗")
print("  ║  IF YOU SEE 429 ERRORS                                   ║")
print("  ║                                                          ║")
print("  ║  Garmin has IP-blocked your network for ~24–48 hours.    ║")
print("  ║  The block affects all login strategies.                 ║")
print("  ║                                                          ║")
print("  ║  Fix: authenticate from a different IP:                  ║")
print("  ║    • Turn on iPhone Personal Hotspot                     ║")
print("  ║    • Connect Windows Wi-Fi to the hotspot                ║")
print("  ║    • Run this script again                               ║")
print("  ║                                                          ║")
print("  ║  Tokens are saved after one successful auth, so          ║")
print("  ║  you will NEVER need to authenticate again               ║")
print("  ║  unless the refresh token also expires (~1 year).        ║")
print("  ╚══════════════════════════════════════════════════════════╝")
print()

def prompt_mfa() -> str:
    return input("Garmin OTP code: ").strip()

try:
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    # Pass tokenstore so the library auto-saves tokens on success.
    client.login(tokenstore=str(TOKEN_DIR))
except GarminConnectAuthenticationError as exc:
    print(f"\n✗ Authentication failed (check credentials):\n  {exc}")
    raise SystemExit(1)
except GarminConnectTooManyRequestsError:
    print()
    print("✗ All 5 strategies returned 429 because the IP is rate-limited.")
    print("  Follow the instructions in the box above.")
    raise SystemExit(1)
except Exception as exc:
    print(f"\n✗ Login failed: {exc}")
    raise SystemExit(1)

# ── Verify ────────────────────────────────────────────────────────────────────
try:
    info = client.get_full_name()
    print(f"\n✓ Logged in as: {info}")
except Exception as exc:
    print(f"\nLogin succeeded but profile fetch failed: {exc}")

if TOKEN_FILE.exists():
    print(f"✓ Tokens saved to {TOKEN_FILE}")
    print("  Future runs will load from this file without needing SSO.")
else:
    # garminconnect should have saved, but fall back to manual dump
    try:
        client.client.dump(str(TOKEN_DIR))
        print(f"✓ Tokens saved to {TOKEN_FILE}")
    except Exception as exc:
        print(f"✗ Could not save tokens: {exc}")
        raise SystemExit(1)

print("\nDone. Restart the app and click the sync button.")
