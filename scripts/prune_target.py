#!/usr/bin/env python3
"""Prune a Cargo target directory before it is saved to the cache.

Requires Python 3.14+. Standard library only.

A whole ``target/`` directory is mostly the *workspace's own* build
products: huge, and stale the moment the source moves. Third-party
dependency artifacts are the part worth caching. This script removes,
for each target directory given:

* every ``incremental/`` directory (never reusable across machines),
* ``.fingerprint`` entries, ``build`` script outputs and ``deps``
  artifacts that belong to the workspace's own packages (names derived
  from the given manifests, including workspace members).

Artifacts of third-party crates are left untouched. All deletions are
confined to the resolved target directories; ``--dry-run`` reports
without deleting.
"""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from pathlib import Path


def local_package_names(workspace: Path, manifests: list[Path]) -> set[str]:
    names: set[str] = set()
    for manifest in manifests:
        if not manifest.is_file():
            continue
        with manifest.open("rb") as handle:
            data = tomllib.load(handle)
        package = data.get("package")
        if isinstance(package, dict) and isinstance(package.get("name"), str):
            names.add(package["name"])
        workspace_cfg = data.get("workspace")
        if isinstance(workspace_cfg, dict):
            members = workspace_cfg.get("members") or []
            for member in members:
                if not isinstance(member, str) or "*" in member:
                    continue
                names |= local_package_names(workspace, [manifest.parent / member / "Cargo.toml"])
    return names


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _belongs_to(stem: str, names: set[str]) -> bool:
    """True when an artifact file/dir stem belongs to a local package."""
    for name in names:
        underscored = name.replace("-", "_")
        for form in (name, underscored):
            if stem == form or stem == f"lib{form}":
                return True
            if stem.startswith(f"{form}-") or stem.startswith(f"lib{form}-"):
                return True
    return False


_ARTIFACT_EXTS = {
    ".rlib",
    ".rmeta",
    ".so",
    ".dylib",
    ".dll",
    ".pdb",
    ".d",
    ".exp",
    ".lib",
    ".a",
    ".o",
    ".bc",
    ".json",
}


def _stem_of(filename: str) -> str:
    stem = filename
    while True:
        suffix = Path(stem).suffix
        if suffix in _ARTIFACT_EXTS and suffix:
            stem = stem[: -len(suffix)]
        else:
            return stem


def prune(target_dir: Path, names: set[str], dry_run: bool = False) -> int:
    """Remove local artifacts under target_dir. Returns bytes freed."""
    target_dir = target_dir.resolve()
    if not target_dir.is_dir():
        return 0
    freed = 0
    candidates: list[Path] = []
    for root, dirs, files in os.walk(target_dir):
        root_path = Path(root)
        if "incremental" in dirs:
            candidates.append(root_path / "incremental")
            dirs.remove("incremental")
        section = root_path.name
        if section in (".fingerprint", "build"):
            for entry in dirs:
                if _belongs_to(entry, names):
                    candidates.append(root_path / entry)
            dirs[:] = [d for d in dirs if not _belongs_to(d, names)]
        elif section == "deps":
            for name in files:
                if _belongs_to(_stem_of(name), names):
                    candidates.append(root_path / name)
    for path in candidates:
        resolved = path.resolve()
        if target_dir not in resolved.parents and resolved != target_dir:
            continue  # never delete outside the target directory
        freed += _size(resolved)
        if not dry_run:
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
            else:
                resolved.unlink(missing_ok=True)
    return freed


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--targets", required=True, help="newline/comma separated")
    parser.add_argument("--manifests", default="Cargo.toml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    manifests = []
    for chunk in args.manifests.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk and "*" not in chunk:
            manifests.append(workspace / chunk)
    names = local_package_names(workspace, manifests)

    total = 0
    for chunk in args.targets.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk:
            total += prune(workspace / chunk, names, dry_run=args.dry_run)
    print(f"local packages: {sorted(names)}")
    print(f"pruned bytes: {total} ({total / 1_000_000:.1f} MB)")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"pruned-bytes={total}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
