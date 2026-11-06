#!/usr/bin/env python3
"""Compute platform-independent dependency hashes and cache keys for Cargo.

Requires Python 3.14+. Standard library only.

Design notes
------------
* Manifests and lockfiles are *parsed* as TOML, never regex-edited, so
  comments, formatting, key order and CRLF line endings cannot change the
  hash. Only the parsed, canonicalised structure is hashed.
* ``[package] version`` and ``[workspace.package] version`` are dropped:
  a release chord bumps the package version without touching the
  dependency graph, and must not fragment the cache. Dependency version
  *requirements* are kept: changing ``serde = "1.0"`` to ``"1.1"`` rehashes.
* Unordered collections are sorted before hashing: dependency ``features``
  lists, workspace ``members``, lockfile ``package`` entries and each lock
  package's ``dependencies`` list.
* Lockfiles are optional. With no lockfile the hash covers the manifests
  only and ``lock_present`` is reported as ``false``. The build itself
  still decides whether to pass ``--locked``; the cache key never forces it.
* The dependency hash deliberately contains no OS, architecture, path or
  toolchain data, so the same inputs hash identically on Linux, Windows
  and macOS. Platform data enters only the *target* cache key, next to a
  separate profile hash covering the toolchain label, ``RUSTFLAGS``,
  ``CARGO_PROFILE_*`` variables and cargo config files, so a flag change
  can never silently restore artifacts built with different flags.
* ``KEY_SCHEMA`` is embedded in every key. Bump it when the normalisation
  rules change so old caches are abandoned instead of mis-restored.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

KEY_SCHEMA = 1
_SORTED_LIST_KEYS = {"features", "members", "default-members", "keywords", "categories"}
_PROFILE_ENV_PREFIXES = ("CARGO_PROFILE_",)
_PROFILE_ENV_NAMES = {
    "RUSTFLAGS",
    "RUSTDOCFLAGS",
    "CARGO_INCREMENTAL",
    "CARGO_BUILD_TARGET",
    "CARGO_ENCODED_RUSTFLAGS",
}


def _norm(node: object, key_hint: str = "") -> object:
    """Recursively canonicalise a parsed TOML value."""
    if isinstance(node, dict):
        return {key: _norm(node[key], key) for key in sorted(node)}
    if isinstance(node, list):
        items = [_norm(item) for item in node]
        if key_hint in _SORTED_LIST_KEYS and all(isinstance(item, str) for item in items):
            items.sort()
        return items
    if isinstance(node, str):
        # Path separators are platform syntax, not dependency content.
        return node.replace("\\", "/")
    return node


def normalize_manifest(data: dict) -> dict:
    """Canonicalise one Cargo.toml: drop own versions, sort the rest."""
    data = _norm(data)  # type: ignore[assignment]
    package = data.get("package")
    if isinstance(package, dict):
        package.pop("version", None)
    workspace = data.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("package"), dict):
        workspace["package"].pop("version", None)
    return data  # type: ignore[return-value]


def normalize_lock(data: dict) -> dict:
    """Canonicalise one Cargo.lock: drop local package versions, sort packages."""
    packages = []
    for entry in data.get("package", []):
        entry = dict(entry)
        if "source" not in entry:
            # Local/path crates: their version is the workspace's own
            # version, which chords independently of the dependency graph.
            entry.pop("version", None)
        deps = entry.get("dependencies")
        if isinstance(deps, list) and all(isinstance(dep, str) for dep in deps):
            entry["dependencies"] = sorted(deps)
        packages.append(_norm(entry))
    packages.sort(
        key=lambda pkg: (
            str(pkg.get("name", "")),
            str(pkg.get("source", "")),
            str(pkg.get("checksum", "")),
        )
    )
    return {
        "version": data.get("version"),
        "package": packages,
        "metadata": _norm(data.get("metadata", {})),
    }


def _split_patterns(raw: str) -> list[str]:
    patterns: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk:
            patterns.append(chunk)
    return patterns


def _expand(workspace: Path, raw: str) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in _split_patterns(raw):
        for match in glob.glob(pattern, root_dir=workspace, recursive=True):
            path = workspace / match
            if path.is_file():
                found[str(path)] = path
    return [found[key] for key in sorted(found)]


def _load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def dependency_payload(workspace: Path, manifests: str, lockfiles: str) -> dict:
    """Build the canonical payload whose hash is the dependency hash."""
    payload: dict = {"manifests": {}, "locks": {}, "lock_present": False}
    for path in _expand(workspace, manifests):
        rel = path.relative_to(workspace).as_posix()
        payload["manifests"][rel] = normalize_manifest(_load_toml(path))
    for path in _expand(workspace, lockfiles):
        rel = path.relative_to(workspace).as_posix()
        payload["locks"][rel] = normalize_lock(_load_toml(path))
    payload["lock_present"] = bool(payload["locks"])
    return payload


def profile_payload(workspace: Path, toolchain: str, rustc_version: str, environ: dict) -> dict:
    """Everything that changes codegen but is not a dependency."""
    env: dict[str, str] = {}
    for name in sorted(environ):
        if name in _PROFILE_ENV_NAMES or name.startswith(_PROFILE_ENV_PREFIXES):
            env[name] = environ[name]
    config: dict[str, object] = {}
    for rel in (".cargo/config.toml", ".cargo/config"):
        path = workspace / rel
        if path.is_file():
            config[rel] = _norm(_load_toml(path))
    toolchain_file: dict[str, object] = {}
    toml_path = workspace / "rust-toolchain.toml"
    if toml_path.is_file():
        toolchain_file["rust-toolchain.toml"] = _norm(_load_toml(toml_path))
    plain_path = workspace / "rust-toolchain"
    if plain_path.is_file():
        toolchain_file["rust-toolchain"] = plain_path.read_text(encoding="utf-8").strip()
    return {
        "toolchain": toolchain,
        "rustc_version": rustc_version,
        "env": env,
        "cargo_config": config,
        "toolchain_file": toolchain_file,
    }


def _hash(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


def _slug(raw: str) -> str:
    out = []
    for ch in raw.strip().lower():
        out.append(ch if (ch.isalnum() or ch in "._-") else "-")
    return "".join(out).strip("-") or "default"


def fallback_payload(workspace: Path, manifests: str, lockfiles: str) -> dict:
    """Raw per-file hashes, used when TOML parsing fails.

    The cache is an optimisation and must never break a build, so a
    manifest the parser rejects degrades to plain file hashing (the
    pre-action behaviour) instead of failing the job.
    """
    files: dict[str, str] = {}
    for pattern_args in (manifests, lockfiles):
        for path in _expand(workspace, pattern_args):
            rel = path.relative_to(workspace).as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"fallback": True, "files": files, "lock_present": False}


def compute_keys(
    workspace: Path,
    manifests: str,
    lockfiles: str,
    prefix: str,
    profile_name: str,
    target_triple: str,
    toolchain: str,
    os_name: str,
    arch: str,
    rustc_version: str = "",
    environ: dict | None = None,
    fallback: bool = False,
) -> dict[str, str]:
    if fallback:
        dep_hash = _hash(fallback_payload(workspace, manifests, lockfiles))
    else:
        dep_hash = _hash(dependency_payload(workspace, manifests, lockfiles))
    try:
        prof = profile_payload(workspace, toolchain, rustc_version, environ or {})
    except Exception:
        if not fallback:
            raise
        prof = {
            "toolchain": toolchain,
            "rustc_version": rustc_version,
            "env": {},
            "cargo_config": {},
            "toolchain_file": {},
        }
    prof["profile_name"] = profile_name
    profile_hash = _hash(prof)

    cargo_home = (environ or {}).get("CARGO_HOME") or str(Path.home() / ".cargo")
    registry_paths = "\n".join(
        str(Path(cargo_home) / part) for part in ("registry/index", "registry/cache", "git/db")
    )

    os_slug, arch_slug = _slug(os_name), _slug(arch)
    triple_slug = _slug(target_triple) if target_triple else "host"
    registry_key = f"{prefix}-v{KEY_SCHEMA}-registry-{dep_hash}"
    target_base = f"{prefix}-v{KEY_SCHEMA}-target-{os_slug}-{arch_slug}-{triple_slug}"
    target_key = f"{target_base}-{profile_hash[:16]}-{dep_hash}"
    if len(target_key) > 512 or len(registry_key) > 512:
        raise ValueError("cache key exceeds the 512 character limit")
    return {
        "dep_hash": dep_hash,
        "profile_hash": profile_hash,
        "registry_key": registry_key,
        "target_key": target_key,
        "registry_restore_keys": f"{prefix}-v{KEY_SCHEMA}-registry-",
        "target_restore_keys": (f"{target_base}-{profile_hash[:16]}-\n{target_base}-"),
        "registry_paths": registry_paths,
        "key_schema": str(KEY_SCHEMA),
        "fallback": "true" if fallback else "false",
    }


def _rustc_version() -> str:
    try:
        out = subprocess.run(["rustc", "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--manifests", default="Cargo.toml")
    parser.add_argument("--lockfiles", default="Cargo.lock")
    parser.add_argument("--prefix", default="cargo")
    parser.add_argument("--profile-name", default="ci")
    parser.add_argument("--target-triple", default="")
    parser.add_argument("--toolchain", default="")
    parser.add_argument("--os", default="")
    parser.add_argument("--arch", default="")
    parser.add_argument("--fallback", action="store_true")
    args = parser.parse_args(argv)

    result = compute_keys(
        workspace=Path(args.workspace).resolve(),
        manifests=args.manifests,
        lockfiles=args.lockfiles,
        prefix=_slug(args.prefix),
        profile_name=args.profile_name,
        target_triple=args.target_triple,
        toolchain=args.toolchain,
        os_name=args.os,
        arch=args.arch,
        rustc_version=_rustc_version(),
        environ=dict(os.environ),
        fallback=args.fallback,
    )
    print(json.dumps(result, sort_keys=True))

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            for name in (
                "dep_hash",
                "profile_hash",
                "registry_key",
                "target_key",
                "key_schema",
                "fallback",
            ):
                handle.write(f"{name.replace('_', '-')}={result[name]}\n")
            for name in ("registry_restore_keys", "target_restore_keys", "registry_paths"):
                delim = f"EOF_{name}"
                handle.write(f"{name.replace('_', '-')}<<{delim}\n{result[name]}\n{delim}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "### cargo-cache-action keys\n\n"
                f"- dep-hash: `{result['dep_hash']}`\n"
                f"- profile-hash: `{result['profile_hash']}`\n"
                f"- registry key: `{result['registry_key']}`\n"
                f"- target key: `{result['target_key']}`\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
