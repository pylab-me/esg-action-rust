# esg-action-rust

A GitHub Actions cache for Cargo workspaces whose cache keys are computed by
parsing `Cargo.toml` and `Cargo.lock`, not by hashing raw files. The action is
a set of composite actions whose logic is written in Python 3.14 (standard
library only); the JavaScript actions `actions/cache/restore` and
`actions/cache/save` are used as thin storage wrappers, so archives, upload and
download behaviour remain GitHub's maintained implementation while every key,
pruning and retention decision stays in this repository.

Replace `<owner>` in the examples with the account that hosts this repository.

## Why

Hashing lockfiles directly, the conventional `actions/cache` setup, has three
failure modes for a workspace that releases by bumping its own version:

1. A version chord changes `[package] version` and rewrites the lockfile entry
   for the local crate, so the cache key changes although no dependency moved.
2. A project that does not commit its lockfile has nothing stable to hash.
3. The same dependency graph spelled differently — reordered entries,
   reformatted tables, CRLF line endings, another operating system — produces
   different hashes for identical content.

This action parses the TOML once and hashes a canonical form instead, which
removes all three.

## The three actions

| Action | Reference | Purpose |
| --- | --- | --- |
| restore | `pylab-me/esg-action-rust@v0.1.0` | Compute keys, restore the registry and target caches |
| save | `pylab-me/esg-action-rust/save@v0.1.0` | Prune local build products, save both caches under the same keys |
| cleanup | `pylab-me/esg-action-rust/cleanup@v0.1.0` | Delete expired caches owned by this action |

Composite actions have no post-job hook — that is a JavaScript action
capability — so save is an explicit second step at the end of the job rather
than an automatic post phase. The split is what makes pruning possible: the
target directory is cleaned after the build and before the save, inside the
same job.

## Usage

```yaml
steps:
  - uses: actions/checkout@v7
    with:
      persist-credentials: false

  - name: Restore Cargo caches
    id: cargo_cache
    uses: pylab-me/esg-action-rust@v0.1.0
    with:
      key-prefix: my-project-ci
      profile-name: ci                 # ci | release | debug | any label
      toolchain: ${{ env.RUST_TOOLCHAIN }}
      target-triple: ${{ env.TARGET }} # optional
      runner-os: ${{ runner.os }}
      runner-arch: ${{ runner.arch }}
      manifests: |
        Cargo.toml
        bench/Cargo.toml
        bench/*/Cargo.toml
      lockfiles: |
        Cargo.lock
        bench/Cargo.lock

  - run: cargo test --workspace

  - name: Save Cargo caches
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    uses: pylab-me/esg-action-rust/save@v0.1.0
    with:
      registry-key: ${{ steps.cargo_cache.outputs.registry-key }}
      target-key: ${{ steps.cargo_cache.outputs.target-key }}
      registry-paths: ${{ steps.cargo_cache.outputs.registry-paths }}
```

Complete workflows live in [`examples/`](examples/): a two-workspace CI
migration and a scheduled cleanup.

### Restore inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `workspace` | `.` | Directory containing the Cargo workspace |
| `manifests` | `Cargo.toml` | Manifests or globs, one per line or comma separated |
| `lockfiles` | `Cargo.lock` | Lockfiles or globs; missing files are ignored |
| `key-prefix` | `cargo` | First segment of every key; keep one prefix per project |
| `profile-name` | `ci` | Human-readable profile label |
| `target-triple` | host | Rust target triple segment of the target key |
| `toolchain` | — | Toolchain label, also hashed into the profile hash |
| `runner-os` / `runner-arch` | — | Platform segments; pass `${{ runner.os }}` / `${{ runner.arch }}` |
| `target-paths` | `target` | Target directories, one per line |
| `enable-cross-os-archive` | `false` | Share the registry archive across operating systems |

Outputs: `dep-hash`, `profile-hash`, `registry-key`, `target-key`,
`registry-paths` (resolved from `CARGO_HOME`, so the official `rust`
container images with `CARGO_HOME=/usr/local/cargo` are handled correctly),
`registry-cache-hit`, `target-cache-hit`, `cache-hit` and `fallback`.

### Save inputs

`registry-key` and `target-key` are required and must be the restore outputs.
`registry-paths` may be omitted and is then resolved from `CARGO_HOME` again.
`prune` (default `true`) removes local artifacts before saving; `manifests`
supplies the manifests from which local package names are derived.
`save-registry` and `save-target` toggle each cache independently.

## How the keys are built

```
dep-hash     = sha256(canonical TOML of all manifests + normalised lockfiles)
profile-hash = sha256(toolchain label, rustc --version, RUSTFLAGS,
                      CARGO_PROFILE_* variables, .cargo/config(.toml),
                      rust-toolchain(.toml), profile-name)
registry-key = <prefix>-v1-registry-<dep-hash>
target-key   = <prefix>-v1-target-<os>-<arch>-<triple>-<profile-hash[..16]>-<dep-hash>
```

Normalisation rules:

- `[package] version` and `[workspace.package] version` are removed. A chord
  therefore never changes the hash. Dependency version *requirements* are
  kept: changing `serde = "1.0"` to `"1.1"` rehashes, as it must.
- In lockfiles, packages without a `source` are local crates and lose their
  `version`; all packages are sorted by name, source and checksum, and each
  package's `dependencies` list is sorted.
- Dependency `features` lists, workspace `members` and all mapping keys are
  sorted. Comments, formatting and CRLF endings disappear by construction,
  because the hash input is the parsed structure serialised as canonical JSON.
- The dependency hash contains no OS, architecture, path or toolchain data:
  the same inputs produce the same hash on Linux, Windows and macOS. Platform
  data appears only in the target key, alongside the profile hash, so changing
  `RUSTFLAGS` or a release profile variable can never restore artifacts built
  with different flags.
- Lockfiles are optional. Without one, the hash covers the manifests alone
  and the step summary records that no lock was present. Whether the build
  itself runs with `--locked` stays the workflow's own decision.
- The `v1` segment is the key schema version. It changes whenever the
  normalisation rules change, abandoning old caches instead of mis-restoring
  them.

Restore keys fall back in order: the exact target key, the same platform and
profile with any dependency hash, then the same platform with any profile.
The registry falls back to any registry cache for the prefix.

## Pruning

Before saving, `scripts/prune_target.py` removes from each target directory:

- every `incremental/` directory,
- `.fingerprint` entries, `build` script outputs and `deps` artifacts that
  belong to the workspace's own packages (including workspace members).

Third-party dependency artifacts — the part of `target/` that is expensive to
rebuild and safe to reuse — are kept. The step summary and the save action's
`pruned-bytes` output report how much was removed, and pruning failure degrades
to saving the directory unpruned rather than failing the job.

## Cleanup

`actions/cache` saves in its post phase, so a cache created by a job cannot be
deleted inside that same job; cleanup is a separate workflow by design:

```yaml
on:
  schedule:
    - cron: '17 3 * * 1'
  workflow_dispatch:

permissions:
  actions: write
  contents: read

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: pylab-me/esg-action-rust/cleanup@v0.1.0
        with:
          max-age-hours: '24'
          key-prefix: my-project-ci-
          dry-run: 'false'
```

A cache is deleted only when all of the following hold:

- its key starts with the configured prefix (other tools' caches are never
  touched),
- its last access — creation time as fallback — is older than
  `max-age-hours` (**24 hours is a supported setting**, as is any other
  value),
- it is not among the newest `keep-latest-per-family` entries (default 1) of
  its key family, where a family is the key without its trailing hash
  segment. This protects the newest warm cache of every line from a strict
  age rule; set the keep count to `0` for an unconditional age cutoff.

An optional `max-total-gb` budget additionally deletes the
least-recently-accessed owned caches until the repository is back under
budget. `dry-run` defaults to `true` and only reports. GitHub's own eviction
(7 days unused, per-repository quota) continues to apply underneath; this
action exists for the superseded generations and stricter age limits the
platform does not offer.

## Python 3.14 bootstrap

Every action first probes for a Python of at least 3.14 (`python3.14`,
`python3`, `python` on PATH) and only provisions one through
`actions/setup-python@v5` with `python-version: '3.14'` when the probe finds
nothing — for example inside the `rust:slim` container images, which ship no
Python at all. All scripts use only the standard library (`tomllib`,
`urllib`), so there is no pip install step and no supply chain beyond the
actions referenced here.

If manifest parsing fails outright, key generation falls back to hashing the
raw manifest and lockfile bytes, emits a workflow warning and sets the
`fallback` output. A cache must never be the reason a build fails.

## Retention and safety recommendations

- Save only from the default branch and version tags, as in the example:
  pull requests restore the main cache but never write one, which keeps fork
  pull requests from polluting or poisoning the shared caches and reserves
  the repository quota for caches that outlive a branch.
- Give only the cleanup job `actions: write`; build jobs need nothing beyond
  the default cache access.
- Pin a release tag (`@v0.1.0`) for production workflows.

## Repository layout

```
action.yml                  restore action (composite)
save/action.yml             save action (composite)
cleanup/action.yml          cleanup action (composite)
scripts/keygen.py           TOML normalisation, profile hash, key construction
scripts/prune_target.py     target directory pruning before save
scripts/cleanup.py          expired-cache selection and deletion (REST API)
scripts/paths.py            CARGO_HOME-aware registry path resolution
tests/                      unit tests and the cross-platform fixture
examples/                   migrated CI workflow and scheduled cleanup
.github/workflows/selftest.yml
```

The self-test runs the unit tests on Python 3.14, parses every YAML file, and
computes the fixture's dependency hash on Ubuntu, Windows and macOS runners,
failing unless all three platforms produce one identical hash.

## Versioning

Releases are tagged `vX.Y.Z` on the `main` branch. Consumers pin the full tag.
The key schema version (`v1` in every key) is versioned separately from the
action release and changes only when normalisation semantics change.

## Licence

MIT
