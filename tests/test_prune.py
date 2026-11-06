"""Pruning: local artifacts go, third-party artifacts stay."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import prune_target  # noqa: E402


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-prune-"))
        self.target = self.tmp / "target" / "debug"
        deps = self.target / "deps"
        deps.mkdir(parents=True)
        (self.target / "incremental").mkdir()
        (self.target / "incremental" / "junk").write_bytes(b"x" * 10)
        fingerprint = self.target / ".fingerprint"
        (fingerprint / "my-app-abc123").mkdir(parents=True)
        (fingerprint / "serde-def456").mkdir(parents=True)
        (deps / "libmy_app-abc123.rlib").write_bytes(b"x" * 10)
        (deps / "my_app-abc123.d").write_bytes(b"x" * 10)
        (deps / "libserde-def456.rlib").write_bytes(b"y" * 10)
        build = self.target / "build"
        (build / "my-app-abc123").mkdir(parents=True)
        (build / "serde-def456").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prune_removes_only_local(self):
        freed = prune_target.prune(self.tmp / "target", {"my-app"})
        self.assertGreater(freed, 0)
        self.assertFalse((self.target / "incremental").exists())
        self.assertFalse((self.target / ".fingerprint" / "my-app-abc123").exists())
        self.assertFalse((self.target / "deps" / "libmy_app-abc123.rlib").exists())
        self.assertFalse((self.target / "build" / "my-app-abc123").exists())
        self.assertTrue((self.target / ".fingerprint" / "serde-def456").exists())
        self.assertTrue((self.target / "deps" / "libserde-def456.rlib").exists())
        self.assertTrue((self.target / "build" / "serde-def456").exists())

    def test_local_names_from_manifest_and_members(self):
        ws = self.tmp / "ws"
        (ws / "crates" / "inner").mkdir(parents=True)
        (ws / "Cargo.toml").write_text(
            '[package]\nname = "outer"\nversion = "0.1.0"\n\n[workspace]\nmembers = ["crates/inner"]\n',
            encoding="utf-8",
        )
        (ws / "crates" / "inner" / "Cargo.toml").write_text(
            '[package]\nname = "inner"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        names = prune_target.local_package_names(ws, [ws / "Cargo.toml"])
        self.assertEqual(names, {"outer", "inner"})


if __name__ == "__main__":
    unittest.main()
