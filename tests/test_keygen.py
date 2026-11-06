"""Key generation: version insensitivity, sorting, stability, fallback."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYGEN = ROOT / "scripts" / "keygen.py"
FIXTURE = ROOT / "tests" / "fixtures" / "proj"


def run_keygen(workspace: Path, extra_env: dict | None = None) -> dict:
    import os

    env = dict(os.environ)
    env.pop("CARGO_HOME", None)
    # Keep the profile hash free of this machine's rustc so assertions
    # about hashes stay about the inputs under test.
    env.pop("RUSTFLAGS", None)
    if extra_env:
        env.update(extra_env)
    out = subprocess.run(
        [
            sys.executable,
            str(KEYGEN),
            "--workspace",
            str(workspace),
            "--os",
            "Linux",
            "--arch",
            "X64",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def variant(tmp: Path, manifest_text: str, lock_text: str | None) -> Path:
    proj = tmp / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "Cargo.toml").write_text(manifest_text, encoding="utf-8")
    if lock_text is not None:
        (proj / "Cargo.lock").write_text(lock_text, encoding="utf-8")
    return proj


class KeygenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-test-"))
        self.base_manifest = (FIXTURE / "Cargo.toml").read_text(encoding="utf-8")
        self.base_lock = (FIXTURE / "Cargo.lock").read_text(encoding="utf-8")
        self.base = run_keygen(FIXTURE)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chord_bump_does_not_change_dep_hash(self):
        proj = variant(
            self.tmp,
            self.base_manifest.replace("0.7.17", "0.7.18"),
            self.base_lock.replace("0.7.17", "0.7.18"),
        )
        self.assertEqual(run_keygen(proj)["dep_hash"], self.base["dep_hash"])

    def test_reorder_and_reformat_do_not_change_dep_hash(self):
        manifest = (
            "# a comment\n"
            "[dependencies]\n"
            'tokio = "1"\n'
            'serde = { version = "1.0", features = ["alloc", "derive"] }\n\n'
            "[package]\n"
            'edition = "2021"\nversion = "9.9.9"\nname = "fixture-app"\n'
        )
        proj = variant(self.tmp, manifest, self.base_lock)
        self.assertEqual(run_keygen(proj)["dep_hash"], self.base["dep_hash"])

    def test_crlf_does_not_change_dep_hash(self):
        proj = variant(
            self.tmp, self.base_manifest.replace("\n", "\r\n"), self.base_lock.replace("\n", "\r\n")
        )
        self.assertEqual(run_keygen(proj)["dep_hash"], self.base["dep_hash"])

    def test_dependency_requirement_change_rehashes(self):
        proj = variant(
            self.tmp,
            self.base_manifest.replace('version = "1.0"', 'version = "1.1"'),
            self.base_lock,
        )
        self.assertNotEqual(run_keygen(proj)["dep_hash"], self.base["dep_hash"])

    def test_registry_checksum_change_rehashes(self):
        proj = variant(self.tmp, self.base_manifest, self.base_lock.replace("0001", "0009"))
        self.assertNotEqual(run_keygen(proj)["dep_hash"], self.base["dep_hash"])

    def test_missing_lockfile_still_hashes(self):
        proj = variant(self.tmp, self.base_manifest, None)
        result = run_keygen(proj)
        self.assertEqual(len(result["dep_hash"]), 64)
        self.assertNotEqual(result["dep_hash"], self.base["dep_hash"])
        # Deterministic: a second run agrees.
        self.assertEqual(run_keygen(proj)["dep_hash"], result["dep_hash"])

    def test_profile_env_changes_profile_hash_not_dep_hash(self):
        changed = run_keygen(FIXTURE, {"RUSTFLAGS": "-C target-cpu=native"})
        self.assertEqual(changed["dep_hash"], self.base["dep_hash"])
        self.assertNotEqual(changed["profile_hash"], self.base["profile_hash"])
        self.assertNotEqual(changed["target_key"], self.base["target_key"])
        self.assertEqual(changed["registry_key"], self.base["registry_key"])

    def test_keys_have_platform_only_where_it_belongs(self):
        self.assertNotIn("linux", self.base["registry_key"])
        self.assertIn("linux", self.base["target_key"])
        self.assertLessEqual(len(self.base["target_key"]), 512)


if __name__ == "__main__":
    unittest.main()
