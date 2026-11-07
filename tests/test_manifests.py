"""Structural checks on the three composite action manifests.

Plain-text checks on purpose: the test environment needs no YAML
library, and the properties asserted here are exactly the ones GitHub's
manifest template validation enforces -- a workflow YAML parser alone
did not catch the v0.1.0 defect, where literal context expressions in
input descriptions made the action fail to load on GitHub.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = (ROOT / "action.yml", ROOT / "save" / "action.yml", ROOT / "cleanup" / "action.yml")


def manifest_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ManifestTests(unittest.TestCase):
    def test_all_manifests_are_composite(self):
        for path in MANIFESTS:
            self.assertIn("using: composite", manifest_text(path), path)

    def test_no_template_expressions_in_descriptions(self):
        # GitHub template-validates every ${{ }} in a manifest, including
        # inside description strings, and most contexts are unavailable
        # there. Descriptions must stay plain prose.
        for path in MANIFESTS:
            for line in manifest_text(path).splitlines():
                if "description:" in line:
                    self.assertNotIn("${{", line, (path, line))

    def test_restore_toggles_gate_both_restore_steps(self):
        text = manifest_text(ROOT / "action.yml")
        self.assertIn("restore-registry:", text)
        self.assertIn("restore-target:", text)
        self.assertIn("if: inputs.restore-registry == 'true'", text)
        self.assertIn("if: inputs.restore-target == 'true'", text)
        self.assertEqual(text.count("actions/cache/restore@v6"), 2)

    def test_cleanup_default_age_is_48_hours(self):
        text = manifest_text(ROOT / "cleanup" / "action.yml")
        self.assertIn("default: '48'", text)

    def test_setup_uses_rustup_and_declares_outputs(self):
        text = manifest_text(ROOT / "setup" / "action.yml")
        self.assertIn("rustup", text)
        self.assertIn("toolchain install", text)
        self.assertNotIn("dtolnay", text)
        for output in ("toolchain:", "target:", "rustc-version:"):
            self.assertIn(output, text)

    def test_save_toggles_gate_both_save_steps(self):
        text = manifest_text(ROOT / "save" / "action.yml")
        self.assertIn("if: inputs.save-registry == 'true'", text)
        self.assertIn("if: inputs.save-target == 'true'", text)
        self.assertEqual(text.count("actions/cache/save@v6"), 2)


if __name__ == "__main__":
    unittest.main()
