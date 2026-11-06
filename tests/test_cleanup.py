"""Cleanup selection: age rule, prefix isolation, family protection, budget."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cleanup  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def entry(id_: int, key: str, age_hours: float, size: int = 100) -> dict:
    stamp = (NOW - timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z")
    return {
        "id": id_,
        "key": key,
        "ref": "refs/heads/main",
        "size_in_bytes": size,
        "created_at": stamp,
        "last_accessed_at": stamp,
    }


REG = "cargo-v1-registry-" + "a" * 64
REG_OLD = "cargo-v1-registry-" + "b" * 64
TGT = "cargo-v1-target-linux-x64-host-" + "c" * 16 + "-" + "d" * 64


class CleanupTests(unittest.TestCase):
    def select(self, caches, **overrides):
        args = dict(now=NOW, max_age_hours=24.0, key_prefix="cargo-", keep_latest_per_family=1)
        args.update(overrides)
        return cleanup.select_deletions(caches, **args)

    def test_older_than_24h_deleted(self):
        doomed = self.select([entry(1, REG, 25), entry(2, REG_OLD, 1)])
        # REG_OLD is a different hash but the same family (registry);
        # the newest family member is protected, the 25h one is not.
        self.assertEqual([c["id"] for c in doomed], [1])

    def test_younger_than_24h_kept(self):
        self.assertEqual(self.select([entry(1, REG, 23)]), [])

    def test_newest_of_family_protected_even_when_old(self):
        # Sole cache of its family: protected by keep-latest=1 at any age.
        self.assertEqual(self.select([entry(1, REG, 24 * 30)]), [])
        # With keep=0 the same entry is unconditionally expired at 24h.
        doomed = self.select([entry(1, REG, 25)], keep_latest_per_family=0)
        self.assertEqual([c["id"] for c in doomed], [1])

    def test_other_prefixes_never_touched(self):
        other = entry(1, "node-v1-" + "e" * 64, 24 * 30)
        mine = entry(2, REG, 25)
        shield = entry(3, REG_OLD, 1)
        doomed = self.select([other, mine, shield])
        self.assertEqual([c["id"] for c in doomed], [2])

    def test_target_family_strips_only_dep_hash(self):
        old = entry(1, TGT, 48)
        new = entry(2, TGT[:-64] + "f" * 64, 2)
        doomed = self.select([old, new])
        self.assertEqual([c["id"] for c in doomed], [1])

    def test_budget_deletes_oldest_non_protected(self):
        caches = [
            entry(1, REG, 2, size=600),
            entry(2, REG_OLD, 3, size=600),
            entry(3, TGT, 1, size=600),
        ]
        doomed = self.select(caches, max_age_hours=24 * 365, max_total_bytes=1_000)
        self.assertEqual([c["id"] for c in doomed], [2])


if __name__ == "__main__":
    unittest.main()
