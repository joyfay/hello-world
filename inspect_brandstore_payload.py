#!/usr/bin/env python3
"""Inventory a raw brandStorePages payload to find the products the parser misses.

Extracting ASINs by matching known key names only finds products stored the way
you already expect. Everything else — a widget that references a category
instead of a list, a tile shaped differently from its siblings — yields nothing
and disappears silently, so a partial pull is indistinguishable from a store
page that genuinely has no products.

This reads the raw JSON and reports the shape instead of the contents:

  * pagination fields on the response, and whether more results were on offer
  * every widget slot per page, and how many ASINs each one yielded
  * the widget slots that yielded NOTHING, with their internal key structure —
    this is where the missing products are
  * every key anywhere whose name mentions asin/product/category/collection,
    so a container the extractor does not know about shows up by name

Usage
-----
    # after running the client with --dump-dir raw/
    python inspect_brandstore_payload.py raw/
    python inspect_brandstore_payload.py raw/pages_1.json

    # print the full key tree of one empty widget, to see where products live
    python inspect_brandstore_payload.py raw/ --show-empty 3
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from typing import Any, Iterator

ASIN_RE = re.compile(r"^(?:B0[A-Z0-9]{8}|\d{10})$")

# Keys the extractor currently reads. Anything holding products under a key
# outside this set is invisible to it, which is exactly what we are hunting.
KNOWN_ASIN_KEYS = {"productasins", "asin", "asins", "asinlist", "productasin",
                   "asinid", "itemasin", "productasinlist"}

# Key names worth surfacing even when they hold no ASINs — a category- or
# rule-driven widget names its source rather than listing products.
INTERESTING = ("asin", "product", "category", "collection", "browse", "node",
               "rule", "automatic", "dynamic", "criteria", "selection")

PAGINATION_HINTS = ("nexttoken", "token", "total", "count", "hasmore",
                    "cursor", "pagination")


def load(path: str) -> list[tuple[str, Any]]:
    """Read one dump file, or every .json in a directory."""
    if os.path.isdir(path):
        files = sorted(f for f in os.listdir(path) if f.endswith(".json"))
        if not files:
            sys.exit(f"no .json files in {path}")
        return [(f, json.load(open(os.path.join(path, f), encoding="utf-8")))
                for f in files]
    return [(os.path.basename(path), json.load(open(path, encoding="utf-8")))]


def walk(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    """Yield (key, json_path, value) for every key in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            yield key, child, value
            yield from walk(value, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk(item, f"{path}[{i}]")


def count_asins(node: Any) -> int:
    """How many ASINs the current extractor would find in this subtree."""
    total = 0
    for key, _, value in walk(node):
        if key.lower() in KNOWN_ASIN_KEYS:
            values = value if isinstance(value, list) else [value]
            total += sum(1 for v in values
                         if isinstance(v, str) and ASIN_RE.match(v.rsplit(".", 1)[-1].upper()))
    return total


def skeleton(node: Any, depth: int = 0, max_depth: int = 6) -> list[str]:
    """A compact key tree, values replaced by their type."""
    out: list[str] = []
    pad = "  " * depth
    if depth >= max_depth:
        return [f"{pad}..."]
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                out.append(f"{pad}{key}:")
                out.extend(skeleton(value, depth + 1, max_depth))
            else:
                shown = repr(value)
                out.append(f"{pad}{key} = {shown[:70]}")
    elif isinstance(node, list):
        if not node:
            out.append(f"{pad}[] (empty)")
        else:
            out.append(f"{pad}[{len(node)} item(s)], first:")
            out.extend(skeleton(node[0], depth + 1, max_depth))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a raw dump .json, or the --dump-dir directory")
    ap.add_argument("--show-empty", type=int, default=1,
                    help="how many zero-yield widgets to print in full (default 1)")
    ap.add_argument("--max-depth", type=int, default=6)
    args = ap.parse_args()

    dumps = load(args.path)
    empty_widgets: list[tuple[str, str, Any]] = []
    key_census: collections.Counter = collections.Counter()
    key_samples: dict[str, str] = {}
    widget_yield: collections.Counter = collections.Counter()
    widget_seen: collections.Counter = collections.Counter()

    for name, payload in dumps:
        print("=" * 74)
        print(f"{name}")
        print("=" * 74)

        if isinstance(payload, dict):
            print(f"  top-level keys: {list(payload)}")
            for key, jpath, value in walk(payload):
                if any(h in key.lower() for h in PAGINATION_HINTS) and \
                        not isinstance(value, (dict, list)):
                    print(f"  pagination-ish: {jpath} = {value!r}")

        pages = None
        for key in ("brandStorePages", "pages", "storePages"):
            if isinstance(payload, dict) and isinstance(payload.get(key), list):
                pages = payload[key]
                break
        if pages is None:
            pages = next((v for v in (payload.values() if isinstance(payload, dict) else [])
                          if isinstance(v, list) and v and isinstance(v[0], dict)), [])

        print(f"  pages in this response: {len(pages)}")

        for page in pages:
            pid = page.get("pageId", "?")
            content = page.get("content", page)
            widgets = content.get("widgets") if isinstance(content, dict) else None
            total = count_asins(content)
            print(f"\n  page {pid}  ->  {total} ASIN(s) extractable")

            if not isinstance(widgets, list):
                print("    no `widgets` array; content keys: "
                      f"{list(content) if isinstance(content, dict) else type(content).__name__}")
                continue

            for i, widget in enumerate(widgets):
                kinds = [k for k in widget] if isinstance(widget, dict) else []
                kind = next((k for k in kinds if k.lower().endswith("widget")),
                            kinds[0] if kinds else "?")
                got = count_asins(widget)
                widget_seen[kind] += 1
                widget_yield[kind] += got
                flag = "" if got else "   <-- yields nothing"
                print(f"    widgets[{i}] {kind:38} {got:>4} ASIN(s){flag}")
                if not got:
                    empty_widgets.append((pid, f"widgets[{i}].{kind}", widget))

        for key, _, value in walk(payload):
            low = key.lower()
            if any(term in low for term in INTERESTING):
                key_census[key] += 1
                if key not in key_samples:
                    key_samples[key] = repr(value)[:90]

    print("\n" + "=" * 74)
    print("widget types: how many products each yields")
    print("=" * 74)
    for kind, seen in widget_seen.most_common():
        got = widget_yield[kind]
        note = "  <-- never yields anything" if got == 0 else ""
        print(f"  {seen:>4} seen  {got:>5} ASINs  {kind}{note}")

    print("\n" + "=" * 74)
    print("keys mentioning asin/product/category/collection")
    print("=" * 74)
    for key, n in key_census.most_common(40):
        known = "READ" if key.lower() in KNOWN_ASIN_KEYS else "    "
        print(f"  [{known}] {n:>5}x  {key:36} e.g. {key_samples[key]}")
    print("\n  [READ] = the extractor already reads this key.")
    print("  Anything holding products WITHOUT that mark is being dropped —")
    print("  add its key name to ASIN_KEYS in ads_api_brandstore_asins.py.")

    if empty_widgets:
        print("\n" + "=" * 74)
        print(f"zero-yield widgets ({len(empty_widgets)} total) — where the "
              f"missing products are")
        print("=" * 74)
        for pid, label, widget in empty_widgets[:args.show_empty]:
            print(f"\n  page {pid} / {label}")
            for line in skeleton(widget, 2, args.max_depth):
                print(line)
        if len(empty_widgets) > args.show_empty:
            print(f"\n  ... {len(empty_widgets) - args.show_empty} more; "
                  f"raise --show-empty to see them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
