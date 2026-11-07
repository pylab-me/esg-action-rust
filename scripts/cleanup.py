#!/usr/bin/env python3
"""Delete expired GitHub Actions caches for this action's key families.

Requires Python 3.14+. Standard library only (REST API via urllib).

GitHub already evicts caches unused for 7 days and enforces a per-repo
size quota, so this tool targets what the platform does not do:
removing *superseded* generations and enforcing a caller-chosen age
limit -- 48 hours by default -- for caches owned by esg-action-rust.

Selection rules (all must hold for a deletion):

* the cache key starts with the configured prefix,
* its last access (falling back to creation) is older than
  ``--max-age-hours``,
* it is not among the newest ``--keep-latest-per-family`` entries of its
  key family (a family is the key with its trailing hash segment
  removed), so the newest warm cache of every line survives even a
  strict age rule; set the keep count to 0 for an unconditional
  age cutoff.

An optional ``--max-total-gb`` budget additionally deletes the
least-recently-accessed non-protected caches until the repository is
back under budget. ``--dry-run`` (the action default) only reports.
Deletion needs a token with ``actions: write`` on the repository.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

_HASH_TAIL = re.compile(r"-[0-9a-f]{16,64}$")


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def family_of(key: str) -> str:
    return _HASH_TAIL.sub("", key)


def select_deletions(
    caches: list[dict],
    now: datetime,
    max_age_hours: float,
    key_prefix: str,
    keep_latest_per_family: int,
    max_total_bytes: int = 0,
) -> list[dict]:
    """Pure selection logic; separately unit-tested without any API."""

    def last_used(entry: dict) -> datetime:
        return parse_time(entry.get("last_accessed_at") or entry["created_at"])

    owned = [c for c in caches if c["key"].startswith(key_prefix)]
    protected: set[int] = set()
    by_family: dict[str, list[dict]] = {}
    for entry in owned:
        by_family.setdefault(family_of(entry["key"]), []).append(entry)
    for entries in by_family.values():
        entries.sort(key=last_used, reverse=True)
        for entry in entries[:keep_latest_per_family]:
            protected.add(entry["id"])

    max_age_seconds = max_age_hours * 3600
    doomed: dict[int, dict] = {}
    for entry in owned:
        age = (now - last_used(entry)).total_seconds()
        if age > max_age_seconds and entry["id"] not in protected:
            doomed[entry["id"]] = entry

    if max_total_bytes > 0:
        remaining = [c for c in caches if c["id"] not in doomed]
        total = sum(c.get("size_in_bytes", 0) for c in remaining)
        if total > max_total_bytes:
            for entry in sorted(remaining, key=last_used):
                if total <= max_total_bytes:
                    break
                if entry["id"] in protected or not entry["key"].startswith(key_prefix):
                    continue
                doomed[entry["id"]] = entry
                total -= entry.get("size_in_bytes", 0)
    return sorted(doomed.values(), key=last_used)


def _api(token: str, api_url: str, method: str, path: str) -> dict | None:
    request = urllib.request.Request(
        f"{api_url}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
        return json.loads(body) if body else None


def fetch_caches(token: str, api_url: str, repo: str) -> list[dict]:
    caches: list[dict] = []
    page = 1
    while True:
        data = _api(
            token,
            api_url,
            "GET",
            f"/repos/{repo}/actions/caches?per_page=100&page={page}",
        )
        batch = (data or {}).get("actions_caches", [])
        caches.extend(batch)
        if len(batch) < 100:
            return caches
        page += 1


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--key-prefix", default="cargo-")
    parser.add_argument("--keep-latest-per-family", type=int, default=1)
    parser.add_argument("--max-total-gb", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or not args.repo:
        print("GITHUB_TOKEN and a repository (owner/name) are required", file=sys.stderr)
        return 2
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    caches = fetch_caches(token, api_url, args.repo)
    doomed = select_deletions(
        caches,
        now=datetime.now(timezone.utc),
        max_age_hours=args.max_age_hours,
        key_prefix=args.key_prefix,
        keep_latest_per_family=args.keep_latest_per_family,
        max_total_bytes=int(args.max_total_gb * 1_000_000_000),
    )
    freed = sum(c.get("size_in_bytes", 0) for c in doomed)
    mode = "would delete" if args.dry_run else "deleting"
    print(f"{len(caches)} caches total; {mode} {len(doomed)} ({freed / 1_000_000:.1f} MB)")
    for entry in doomed:
        print(f"  {entry['key']} (id={entry['id']}, ref={entry.get('ref')})")
        if not args.dry_run:
            _api(token, api_url, "DELETE", f"/repos/{args.repo}/actions/caches/{entry['id']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### cargo-cache cleanup\n\n"
                f"- mode: {'dry-run' if args.dry_run else 'apply'}\n"
                f"- deleted: {len(doomed)} caches, {freed / 1_000_000:.1f} MB\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
