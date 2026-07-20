#!/usr/bin/env python3
"""Extract Arlo API token, device IDs, and base URL from a .har file.

Usage:
  python3 extract_arlo_token.py ~/Downloads/arlo.har
  eval $(python3 extract_arlo_token.py ~/Downloads/arlo.har --export)
"""
import json
import sys
import re


def extract_info(har_path):
    with open(har_path) as f:
        har = json.load(f)

    token = None
    devices = set()
    base_url = None

    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "arlo.com" not in url:
            continue

        # Find token from auth response
        if "ocapi/accounts/v1/auth" in url and not token:
            resp = entry.get("response", {})
            text = resp.get("content", {}).get("text", "")
            if text:
                try:
                    data = json.loads(text)
                    if "data" in data and isinstance(data["data"], dict):
                        token = data["data"].get("token")
                except (json.JSONDecodeError, KeyError):
                    pass

        # Find token from Authorization header (Bearer style)
        if not token:
            for header in entry["request"]["headers"]:
                if header["name"].lower() == "authorization":
                    value = header["value"]
                    if value.startswith("Bearer "):
                        token = value[7:]
                        break

        # Find API base URL
        if not base_url:
            m = re.match(r'(https://myapi[^/]*arlo\.com)', url)
            if m:
                base_url = m.group(1)

        # Find device IDs from responses
        resp = entry.get("response", {})
        text = resp.get("content", {}).get("text", "")
        if text:
            devices.update(re.findall(r'ALJ\w{10,}', text))

    return token, sorted(devices), base_url


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: extract_arlo_token.py <file.har> [--export]", file=sys.stderr)
        sys.exit(1)

    export_mode = "--export" in sys.argv
    har_path = [a for a in sys.argv[1:] if not a.startswith("--")][0]

    token, devices, base_url = extract_info(har_path)

    if export_mode:
        if token:
            print(f'export ARLO_API_TOKEN="{token}"')
        if devices:
            print(f'export ARLO_DEVICE_ID="{devices[0]}"')
        if base_url:
            print(f'export ARLO_BASE_URL="{base_url}"')
    else:
        print(f"Token: {token[:40]}..." if token else "Token: NOT FOUND")
        print(f"Devices: {', '.join(devices) if devices else 'NOT FOUND'}")
        print(f"Base URL: {base_url or 'NOT FOUND'}")
        if token:
            print(f"\nTo use:")
            print(f'  export ARLO_API_TOKEN="{token}"')
            if devices:
                print(f'  export ARLO_DEVICE_ID="{devices[0]}"')
            if base_url:
                print(f'  export ARLO_BASE_URL="{base_url}"')

    if not token:
        sys.exit(1)
