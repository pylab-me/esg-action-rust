#!/usr/bin/env python3
"""Resolve the cargo registry paths for the current environment.

Requires Python 3.14+. Standard library only. Used by the save action,
which does not recompute keys but still needs the same registry paths
the restore action resolved (honouring CARGO_HOME, e.g. /usr/local/cargo
in the official rust container images, instead of assuming ~/.cargo).
"""

from __future__ import annotations

import os
from pathlib import Path


def registry_paths(environ: dict | None = None) -> str:
    environ = environ or dict(os.environ)
    cargo_home = environ.get("CARGO_HOME") or str(Path.home() / ".cargo")
    return "\n".join(
        str(Path(cargo_home) / part) for part in ("registry/index", "registry/cache", "git/db")
    )


def main() -> int:
    paths = registry_paths()
    print(paths)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"registry-paths<<EOF_PATHS\n{paths}\nEOF_PATHS\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
