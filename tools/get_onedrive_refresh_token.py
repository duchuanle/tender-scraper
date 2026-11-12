"""One-time personal OneDrive device login for the optional upload."""

from __future__ import annotations

import argparse
import sys
import time

import requests


AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "offline_access https://graph.microsoft.com/Files.ReadWrite"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("client_id", help="Microsoft Entra application (client) ID")
    args = parser.parse_args()

    device_response = requests.post(
        f"{AUTHORITY}/devicecode",
        data={"client_id": args.client_id, "scope": SCOPE},
        timeout=30,
    )
    device_response.raise_for_status()
    device = device_response.json()
    print(device["message"])
    print("Waiting for sign-in confirmation...")

    interval = int(device.get("interval", 5))
    deadline = time.monotonic() + int(device["expires_in"])
    while time.monotonic() < deadline:
        time.sleep(interval)
        token_response = requests.post(
            f"{AUTHORITY}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": args.client_id,
                "device_code": device["device_code"],
            },
            timeout=30,
        )
        token = token_response.json()
        if token_response.ok:
            refresh_token = token.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh token returned; offline_access was not granted")
            print("\nONEDRIVE_REFRESH_TOKEN (store this as a deployment secret):\n")
            print(refresh_token)
            return 0

        error = token.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise RuntimeError(token.get("error_description") or error or "Sign-in failed")

    raise RuntimeError("The device login expired before sign-in completed")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
