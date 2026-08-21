#!/usr/bin/env python3
"""Diagnose a 401 from the Amazon Ads API.

A 401 on /v2/profiles has several distinct causes that look identical from the
call site, so this splits the handshake into its two halves and reports on each:

    1. LWA refresh   client_id + client_secret + refresh_token -> access_token
    2. Profiles      access_token + client_id header           -> your profiles

Step 1 failing means the credentials or the refresh token are wrong. Step 1
passing and step 2 failing means the token is fine but the LWA application is
not authorised for the Ads API, or you are calling the wrong region.

It also probes all three regional hosts, because a token minted against a North
American account returns 401 — not 403 — from the EU and FE hosts, which is the
single most misleading failure here.

Usage
-----
    pip install requests

    export ADS_CLIENT_ID=amzn1.application-oa2-client....
    export ADS_CLIENT_SECRET=...
    export ADS_REFRESH_TOKEN=Atzr|...

    python check_ads_api_auth.py
"""

from __future__ import annotations

import json
import os
import sys

import requests

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REGION_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}

# Every LWA endpoint, for the case where the account was created outside the US.
LWA_ALTERNATES = {
    "global (api.amazon.com)": "https://api.amazon.com/auth/o2/token",
    "EU (api.amazon.co.uk)": "https://api.amazon.co.uk/auth/o2/token",
    "FE (api.amazon.co.jp)": "https://api.amazon.co.jp/auth/o2/token",
}


def refresh(client_id: str, client_secret: str, refresh_token: str,
            url: str = LWA_TOKEN_URL) -> tuple[str | None, str]:
    """Exchange the refresh token for an access token. Returns (token, detail)."""
    try:
        resp = requests.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return None, f"network error: {exc}"

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:400]}"

    payload = resp.json()
    token = payload.get("access_token")
    # Echo everything except the secrets, so a missing/odd scope is visible.
    meta = {k: v for k, v in payload.items()
            if k not in ("access_token", "refresh_token")}
    return token, f"ok — {json.dumps(meta)}"


def probe_profiles(host: str, token: str, client_id: str,
                   scope: str | None = None) -> tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": client_id,
        "Accept": "application/json",
    }
    if scope:
        headers["Amazon-Advertising-API-Scope"] = scope
    try:
        resp = requests.get(f"{host}/v2/profiles", headers=headers, timeout=30)
    except requests.RequestException as exc:
        return 0, f"network error: {exc}"
    return resp.status_code, resp.text[:600]


def main() -> int:
    missing = [v for v in ("ADS_CLIENT_ID", "ADS_CLIENT_SECRET", "ADS_REFRESH_TOKEN")
               if not os.environ.get(v)]
    if missing:
        print(f"missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    client_id = os.environ["ADS_CLIENT_ID"]
    client_secret = os.environ["ADS_CLIENT_SECRET"]
    refresh_token = os.environ["ADS_REFRESH_TOKEN"]

    print("=" * 72)
    print("credentials (shape check)")
    print("=" * 72)
    # The prefixes are the fastest way to spot SP-API credentials pasted in by
    # mistake — they look similar but will never authenticate against Ads.
    print(f"  client_id     {client_id[:34]}...  len={len(client_id)}")
    print(f"  refresh_token {refresh_token[:12]}...             len={len(refresh_token)}")
    if not client_id.startswith("amzn1.application-oa2-client."):
        print("  ! client_id does not look like an LWA client id "
              "(expected amzn1.application-oa2-client.<hex>)")
    if not refresh_token.startswith("Atzr|"):
        print("  ! refresh_token does not start with 'Atzr|' — that is what an LWA "
              "refresh token looks like. A token starting with 'Atza|' is an ACCESS "
              "token (valid 1 hour), not a refresh token.")

    print()
    print("=" * 72)
    print("step 1 — LWA token refresh")
    print("=" * 72)
    token, detail = refresh(client_id, client_secret, refresh_token)
    print(f"  {'PASS' if token else 'FAIL'}  {detail}")

    if not token:
        print("\n  Try the other LWA endpoints (relevant if the Amazon account that")
        print("  authorised the app is not a US account):")
        for label, url in LWA_ALTERNATES.items():
            if url == LWA_TOKEN_URL:
                continue
            alt_token, alt_detail = refresh(client_id, client_secret, refresh_token, url)
            print(f"    {'PASS' if alt_token else 'FAIL'}  {label}: {alt_detail[:160]}")
            if alt_token:
                token = alt_token
                print(f"    -> use {url} as your token endpoint")
                break

    if not token:
        print("\nDIAGNOSIS: the refresh never produced an access token, so nothing")
        print("downstream can work. Check, in this order:")
        print("  1. client_secret matches this client_id (a mismatch returns")
        print("     invalid_client)")
        print("  2. the refresh token was issued to THIS client_id — they are bound")
        print("     together; a token from another app returns invalid_grant")
        print("  3. the refresh token has not been revoked (re-consent to reissue)")
        return 1

    print()
    print("=" * 72)
    print("step 2 — GET /v2/profiles on each region")
    print("=" * 72)
    results: dict[str, int] = {}
    for region, host in REGION_HOSTS.items():
        status, body = probe_profiles(host, token, client_id)
        results[region] = status
        mark = "PASS" if status == 200 else "FAIL"
        print(f"  {mark}  {region:3} {status}  {host}")
        if status == 200:
            try:
                profiles = json.loads(body) if body.strip().startswith("[") else []
            except ValueError:
                profiles = []
            for p in profiles[:20]:
                info = p.get("accountInfo", {})
                print(f"          profileId={p.get('profileId')} "
                      f"{p.get('countryCode')} {info.get('type')} "
                      f"{info.get('name')!r}")
            if len(profiles) > 20:
                print(f"          ... and {len(profiles) - 20} more")
        else:
            print(f"          {body[:300]}")

    print()
    print("=" * 72)
    print("diagnosis")
    print("=" * 72)

    if 200 in results.values():
        good = [r for r, s in results.items() if s == 200]
        print(f"  Auth is fine. Your profiles live in: {', '.join(good)}.")
        print(f"  Point your client at {REGION_HOSTS[good[0]]} and pass one of the")
        print("  profileIds above as the Amazon-Advertising-API-Scope header.")
        return 0

    if all(s == 401 for s in results.values()):
        print("  401 from every region, with a valid access token. That combination")
        print("  means the token authenticates but carries no advertising authority:")
        print()
        print("  1. The LWA app is not an Amazon Ads app. SP-API and Ads use separate")
        print("     applications; SP-API credentials return exactly this. Confirm the")
        print("     app is registered under the Amazon Ads developer console.")
        print("  2. The refresh token was minted without the advertising scope. The")
        print("     consent URL must request scope=advertising::campaign_management")
        print("     (Brand Stores content additionally needs the stores scope).")
        print("     Scopes are fixed at consent time — re-authorise to change them.")
        print("  3. The Amazon account that granted consent has no advertising")
        print("     account attached, so there is nothing to authorise against.")
        print()
        print("  Also check the Authorization header is 'Bearer <access_token>' and")
        print("  that Amazon-Advertising-API-ClientId is present — omitting the")
        print("  ClientId header returns 401, not 400.")
        return 1

    print("  Mixed results — see the per-region lines above.")
    for region, status in results.items():
        if status == 403:
            print(f"  {region}: 403 means authenticated but not entitled — the account")
            print(f"      exists in another region, or the app lacks that API's access.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
