#!/usr/bin/env python3
"""Pull the ASINs configured on Amazon Brand Store pages via the Ads API.

Walks the documented chain — Store -> Edition -> Published Version -> Page
Content — and extracts every ASIN referenced by the page's components, tagging
each with the component it came from so fixed product modules can be told apart
from dynamic ones.

    Store            query/brandStores                -> storeId, page list
    Edition          brandStoreEditions               -> editionId
    Publish version  query/brandStoreEditionPublish*  -> storeEditionPublishId
    Page content     query/brandStorePages            -> content -> ASINs

What you get, and what you do not
---------------------------------
Fixed references come back reliably: hand-picked product modules, pinned
product grids, shoppable-image hotspots. Dynamic components (recommended,
best-selling, automatic) return their *rule*, not the products a given shopper
sees, so their ASIN lists are absent or partial by design. Rows are flagged
`dynamic` when the component type looks like one of those — treat those as
"this widget exists", not "these are its products".

Endpoint paths are NOT hard-coded
---------------------------------
Amazon reshaped these endpoints between the 2025 beta and the 2026 GA, and the
docs are the authority, not this file. Every path, method, and request-body key
lives in ENDPOINTS below and can be overridden without touching code:

    python ads_api_brandstore_asins.py --endpoints my_endpoints.json ...

Run with --dump-dir the first time. It writes each raw response to disk, which
is how you find out what the payload actually looks like when a field name here
turns out to be wrong.

Setup
-----
    pip install requests

    export ADS_CLIENT_ID=amzn1.application-oa2-client....
    export ADS_CLIENT_SECRET=...
    export ADS_REFRESH_TOKEN=Atzr|...

Usage
-----
    # discover the store, then pull every page
    python ads_api_brandstore_asins.py --profile-id 2533508533319433 \
        --store-name "U.S. Solid" -o store_asins.csv --dump-dir raw/

    # a store you already know the id of
    python ads_api_brandstore_asins.py --profile-id 2533508533319433 \
        --store-id 29AB675D-2238-4826-BDE2-09EFEAB81AA6 -o store_asins.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REGION_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}

# Every request the chain makes, in one editable place. `body` names the
# request-body keys; the runner fills the values. Override with --endpoints.
ENDPOINTS: dict[str, dict[str, Any]] = {
    "stores": {
        "method": "POST",
        "path": "/adsApi/v1/query/brandStores",
        # maxResults caps at 30 — the API rejects anything larger.
        "body": {"name": "$store_name", "maxResults": 30},
        "items_key": "brandStores",
    },
    "editions": {
        "method": "GET",
        "path": "/adsApi/v1/brandStoreEditions",
        "query": {"brandStoreId": "$store_id"},
        "items_key": "brandStoreEditions",
    },
    "publish_versions": {
        "method": "POST",
        "path": "/adsApi/v1/query/brandStoreEditionPublishVersions",
        "body": {"storeId": "$store_id", "editionId": "$edition_id"},
        "items_key": "brandStoreEditionPublishVersions",
    },
    "pages": {
        "method": "POST",
        "path": "/adsApi/v1/query/brandStorePages",
        "body": {
            "storeId": "$store_id",
            "editionId": "$edition_id",
            "storeEditionPublishId": "$publish_id",
            "pageIds": "$page_ids",
        },
        "items_key": "brandStorePages",
    },
}

ASIN_RE = re.compile(r"^(?:B0[A-Z0-9]{8}|\d{10})$")

# Keys whose values are ASINs, and keys that name the component we are inside.
ASIN_KEYS = {"asin", "asins", "asinlist", "productasin", "asinid", "itemasin"}
TYPE_KEYS = ("componenttype", "widgettype", "moduletype", "type", "name")

# A component whose type matches any of these picks its own products at render
# time, so whatever ASINs it does return are illustrative, not exhaustive.
DYNAMIC_MARKERS = (
    "RECOMMEND", "BESTSELL", "BEST_SELL", "AUTOMATIC", "DYNAMIC",
    "DEAL", "NEW_RELEASE", "TRENDING", "PERSONALIZ",
)


class AdsApiError(RuntimeError):
    """An Ads API call failed in a way retrying will not fix."""


@dataclass
class Row:
    store_id: str
    store_name: str
    page_id: str
    page_title: str
    component_type: str
    component_path: str
    asin: str
    dynamic: bool


@dataclass
class Client:
    client_id: str
    client_secret: str
    refresh_token: str
    profile_id: str
    region: str = "NA"
    dump_dir: str | None = None
    _token: str = field(default="", repr=False)
    _token_expiry: float = field(default=0.0, repr=False)

    @property
    def host(self) -> str:
        try:
            return REGION_HOSTS[self.region.upper()]
        except KeyError:
            raise AdsApiError(f"unknown region {self.region!r}; pick one of {list(REGION_HOSTS)}")

    def _access_token(self) -> str:
        # Tokens last an hour; refresh a minute early to avoid a race at the edge.
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise AdsApiError(f"LWA token refresh failed ({resp.status_code}): {resp.text[:400]}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def call(self, spec: dict[str, Any], subs: dict[str, Any], *, tag: str) -> dict:
        """Issue one request from an ENDPOINTS spec, substituting $placeholders."""
        method = spec.get("method", "POST").upper()
        url = self.host + spec["path"]
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": str(self.profile_id),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(spec.get("headers", {}))

        params = _substitute(spec.get("query", {}), subs) or None
        body = _substitute(spec.get("body", {}), subs) if method != "GET" else None

        for attempt in range(5):
            resp = requests.request(
                method, url, headers=headers, params=params, json=body, timeout=60
            )
            # 429 and 5xx are worth another try; everything else is a real answer.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                wait = 2 ** attempt
                print(f"  {resp.status_code} on {tag}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            break

        if resp.status_code >= 400:
            raise AdsApiError(
                f"{tag} -> {method} {url} failed ({resp.status_code})\n"
                f"  request body: {json.dumps(body, ensure_ascii=False)[:500]}\n"
                f"  response: {resp.text[:800]}\n"
                f"  If this is a 403/404, the path or body keys in ENDPOINTS['{tag}'] "
                f"likely no longer match the docs — override them with --endpoints."
            )

        try:
            data = resp.json()
        except ValueError:
            raise AdsApiError(f"{tag} returned non-JSON: {resp.text[:400]}")

        if self.dump_dir:
            os.makedirs(self.dump_dir, exist_ok=True)
            stamp = f"{tag}_{subs.get('page_ids_label') or subs.get('store_id') or 'x'}"
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stamp)[:120]
            with open(os.path.join(self.dump_dir, f"{safe}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        return data


def _substitute(template: Any, subs: dict[str, Any]) -> Any:
    """Replace '$name' leaves with subs['name'], recursively."""
    if isinstance(template, str) and template.startswith("$"):
        return subs.get(template[1:])
    if isinstance(template, dict):
        return {k: _substitute(v, subs) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, subs) for v in template]
    return template


def _items(payload: dict, key: str) -> list:
    """Pull the item list out of a response without guessing too hard."""
    if isinstance(payload.get(key), list):
        return payload[key]
    # Fall back to the first list of dicts at the top level — response envelopes
    # get renamed between API versions more often than their contents change.
    for value in payload.values():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return value
    return []


def _is_dynamic(component_type: str) -> bool:
    upper = component_type.upper()
    return any(marker in upper for marker in DYNAMIC_MARKERS)


def extract_asins(node: Any, path: str = "", component: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield (asin, component_type, json_path) from a page-content tree.

    The content schema is a nested component tree whose exact shape varies by
    widget, so rather than hard-coding a layout this walks everything and keeps
    the nearest enclosing component type as provenance.
    """
    if isinstance(node, dict):
        current = component
        for key in TYPE_KEYS:
            for actual, value in node.items():
                if actual.lower() == key and isinstance(value, str) and value:
                    current = value
                    break
            if current != component:
                break

        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if key.lower() in ASIN_KEYS:
                yield from _asins_from(value, current, child_path)
            else:
                yield from extract_asins(value, child_path, current)

    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from extract_asins(item, f"{path}[{i}]", component)

    elif isinstance(node, str):
        # Catches `amzn1.asin.B0...` references sitting under unexpected keys.
        if node.startswith("amzn1.asin."):
            candidate = node.rsplit(".", 1)[-1].upper()
            if ASIN_RE.match(candidate):
                yield candidate, component, path


def _asins_from(value: Any, component: str, path: str) -> Iterator[tuple[str, str, str]]:
    """Read one ASIN-bearing field, which may be a string, list, or object."""
    if isinstance(value, str):
        candidate = value.rsplit(".", 1)[-1].upper()
        if ASIN_RE.match(candidate):
            yield candidate, component, path
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _asins_from(item, component, f"{path}[{i}]")
    elif isinstance(value, dict):
        yield from extract_asins(value, path, component)


def resolve_store(client: Client, store_name: str | None, store_id: str | None) -> dict:
    """Find the store to read, by id or by exact name."""
    data = client.call(ENDPOINTS["stores"], {"store_name": store_name or ""}, tag="stores")
    stores = _items(data, ENDPOINTS["stores"]["items_key"])
    if not stores:
        raise AdsApiError(
            f"no store matched name={store_name!r}. The search is a prefix match on the "
            f"public store directory — use the full store name."
        )

    if store_id:
        for store in stores:
            if store.get("storeId") == store_id:
                return store
        raise AdsApiError(f"store {store_id} not in the {len(stores)} result(s) for {store_name!r}")

    if len(stores) > 1:
        names = ", ".join(f"{s.get('storeName')} ({s.get('storeId')})" for s in stores)
        raise AdsApiError(f"{len(stores)} stores matched — pass --store-id to pick one: {names}")

    return stores[0]


def run(client: Client, store_name: str | None, store_id: str | None,
        batch_size: int, out_path: str) -> int:
    store = resolve_store(client, store_name, store_id)
    sid = store["storeId"]
    sname = store.get("storeName", "")
    page_infos = store.get("pageInfos") or []
    print(f"store: {sname} ({sid}) — {len(page_infos)} pages", file=sys.stderr)

    editions = _items(
        client.call(ENDPOINTS["editions"], {"store_id": sid}, tag="editions"),
        ENDPOINTS["editions"]["items_key"],
    )
    if not editions:
        raise AdsApiError(f"store {sid} returned no editions")
    edition_id = editions[0].get("editionId", "default")
    print(f"edition: {edition_id}", file=sys.stderr)

    # The published version is what shoppers actually see. If this endpoint is
    # unavailable, keep going — some deployments accept a page query without it.
    publish_id = None
    try:
        versions = _items(
            client.call(ENDPOINTS["publish_versions"],
                        {"store_id": sid, "edition_id": edition_id}, tag="publish_versions"),
            ENDPOINTS["publish_versions"]["items_key"],
        )
        published = [v for v in versions
                     if str(v.get("status", "")).upper() in ("PUBLISHED", "LIVE", "ACTIVE")]
        chosen = (published or versions or [None])[0]
        if chosen:
            publish_id = chosen.get("storeEditionPublishId") or chosen.get("publishId")
        print(f"publish version: {publish_id}", file=sys.stderr)
    except AdsApiError as exc:
        print(f"publish-version lookup failed, continuing without it:\n{exc}", file=sys.stderr)

    rows: list[Row] = []
    titles = {p["tag"]: p.get("title", "") for p in page_infos}
    page_ids = [p["tag"] for p in page_infos]

    for start in range(0, len(page_ids), batch_size):
        batch = page_ids[start:start + batch_size]
        label = f"{start // batch_size + 1}"
        print(f"pages {start + 1}-{start + len(batch)} of {len(page_ids)} ...",
              file=sys.stderr, flush=True)

        data = client.call(
            ENDPOINTS["pages"],
            {"store_id": sid, "edition_id": edition_id, "publish_id": publish_id,
             "page_ids": batch, "page_ids_label": label},
            tag="pages",
        )

        for page in _items(data, ENDPOINTS["pages"]["items_key"]):
            pid = page.get("pageId", "")
            seen: set[tuple[str, str]] = set()
            for asin, component, path in extract_asins(page.get("content", page)):
                if (asin, component) in seen:
                    continue
                seen.add((asin, component))
                rows.append(Row(sid, sname, pid, titles.get(pid, ""), component,
                                path, asin, _is_dynamic(component)))
            print(f"    {titles.get(pid, pid)}: {len(seen)} ASINs", file=sys.stderr)

        time.sleep(0.5)

    rows.sort(key=lambda r: (r.page_title, r.component_type, r.asin))
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["store_name", "store_id", "page_title", "page_id",
                    "component_type", "component_path", "asin", "dynamic_component"])
        for r in rows:
            w.writerow([r.store_name, r.store_id, r.page_title, r.page_id,
                        r.component_type, r.component_path, r.asin,
                        "yes" if r.dynamic else "no"])

    distinct = {r.asin for r in rows}
    dynamic = {r.asin for r in rows if r.dynamic}
    print(f"\n{len(distinct)} distinct ASINs across {len(page_ids)} pages -> {out_path}",
          file=sys.stderr)
    if dynamic:
        print(f"{len(dynamic)} came from dynamic components — those lists are "
              f"illustrative, not complete.", file=sys.stderr)
    if not rows:
        print("No ASINs found. Run again with --dump-dir and inspect a pages_*.json: "
              "the content schema likely nests ASINs under keys this script does not "
              "recognise (extend ASIN_KEYS).", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile-id", required=True, help="Amazon-Advertising-API-Scope")
    ap.add_argument("--store-name", help="full store name (prefix match on the public directory)")
    ap.add_argument("--store-id", help="storeId, when you already know it")
    ap.add_argument("--region", default="NA", choices=sorted(REGION_HOSTS))
    ap.add_argument("--batch-size", type=int, default=10, help="page ids per pages request")
    ap.add_argument("-o", "--out", default="brandstore_api_asins.csv")
    ap.add_argument("--dump-dir", help="write every raw response here (do this on the first run)")
    ap.add_argument("--endpoints", help="JSON file overriding any part of ENDPOINTS")
    args = ap.parse_args()

    if not (args.store_name or args.store_id):
        ap.error("pass --store-name, --store-id, or both")

    if args.endpoints:
        with open(args.endpoints, encoding="utf-8") as f:
            for name, spec in json.load(f).items():
                ENDPOINTS.setdefault(name, {}).update(spec)

    missing = [v for v in ("ADS_CLIENT_ID", "ADS_CLIENT_SECRET", "ADS_REFRESH_TOKEN")
               if not os.environ.get(v)]
    if missing:
        ap.error(f"missing environment variable(s): {', '.join(missing)}")

    client = Client(
        client_id=os.environ["ADS_CLIENT_ID"],
        client_secret=os.environ["ADS_CLIENT_SECRET"],
        refresh_token=os.environ["ADS_REFRESH_TOKEN"],
        profile_id=args.profile_id,
        region=args.region,
        dump_dir=args.dump_dir,
    )

    try:
        return run(client, args.store_name, args.store_id, args.batch_size, args.out)
    except AdsApiError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
