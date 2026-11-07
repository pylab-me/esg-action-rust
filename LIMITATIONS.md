# Limitations

What esg-action-rust deliberately does not do, and where its guarantees
stop. Everything here is a design boundary, not a defect queue: the suite
stays small by refusing these jobs, and workflows that need more should
combine it with other tools.

## Action model

- **No automatic save.** Composite actions have no post-job hook — that
  capability belongs to JavaScript actions — so saving is an explicit
  second step. If a job is cancelled or fails before the save step runs,
  nothing is saved for that run. Put the save step behind `if: always()`
  if a failed build should still warm the cache.
- **Bash is required.** Composite steps run under bash: Git Bash on
  Windows runners, the job container's bash for container jobs. A
  PowerShell-only environment cannot run these actions.
- **rustup must exist.** The setup action drives rustup and does not
  install it. GitHub-hosted runners and the official `rust` images
  provide rustup; a bare self-hosted runner must install it first.
- **Python 3.14+ must be reachable.** Key generation, pruning and
  cleanup are Python scripts using only the standard library. Each
  action probes the runner for a suitable interpreter and provisions
  one through `actions/setup-python` only when the probe finds
  nothing. On an offline self-hosted runner with no Python 3.14 and no
  way to download one, the actions cannot run.

## Cache keys

- **The hash covers manifests, lockfiles and profile inputs — not
  source code.** A change that alters build behaviour without touching
  `Cargo.toml`, `Cargo.lock`, the toolchain, `RUSTFLAGS`,
  `CARGO_PROFILE_*` variables or the cargo config files does not
  change any key. That includes `build.rs` scripts that read arbitrary
  files or environment variables, and code generation driven by files
  outside the manifests.
- **Path dependencies outside the listed manifests are opaque.**
  Pruning recognises local packages from the manifests it is given and
  their workspace members (glob members such as `crates/*` are followed
  only where the member list is explicit). A path dependency elsewhere
  on disk is treated as third-party content: its artifacts are kept in
  the target cache, and its source changes do not affect the keys.
- **Unparseable TOML degrades the guarantee.** If a manifest or
  lockfile cannot be parsed, key generation falls back to hashing the
  raw file bytes. Caching still works, but version insensitivity and
  formatting stability are lost until the file parses again.
- **The target cache is per platform by construction.** Only the
  registry cache can be shared across operating systems, and only with
  `enable-cross-os-archive` enabled. Compiled artifacts are never
  portable between operating systems, architectures or target triples.
- **Keys are a contract, not a fingerprint of the environment.**
  Anything not listed above — system libraries, linker choice made
  outside cargo config, environment variables read directly by build
  scripts — is invisible to the keys. Workflows that depend on such
  inputs must include them in `key-prefix` or the profile inputs
  themselves.

## GitHub platform limits inherited unchanged

- Cache storage is capped per repository (10 GB by default), entries
  unused for 7 days are evicted, and the least recently accessed
  entries go first over quota. This suite manages keys and retention
  inside those rules; it cannot raise them.
- Cache entries are immutable. Saving under a key that already exists
  is skipped, never overwritten.
- Caches are scoped by branch: a run can restore caches from its own
  branch, its base branch and the default branch, and from no others.
- A cache cannot be deleted by the job that saved it, because
  `actions/cache` saves in its post phase. Cleanup is a separate
  workflow by necessity, not by preference.

## Cleanup

- **Age is measured by last access**, falling back to creation time.
  Any restore counts as access, so an actively restored cache never
  ages out no matter how old its content is.
- **Protection can defeat the budget.** The newest entries of every key
  family are never deleted. If protected entries and caches owned by
  other tools exceed the size budget on their own, the repository
  stays over budget; cleanup reports the state instead of breaking
  the protection rule.
- **Deletion is permanent and eventually consistent.** The REST API
  accepts a deletion immediately, but repository cache listings and
  usage figures can lag behind it. There is no undo; that is why
  `dry-run` defaults to on.
- **Only prefixed keys are eligible.** Caches created by other actions
  or by hand are never listed for deletion, even when they dominate
  the repository's usage.

## Setup

- Toolchain, one target, components, apt packages on Linux, and an
  optional Python — nothing else. It does not install `cargo-*` tools,
  linkers, cross-compilation toolchains beyond the rustup target,
  `sccache` or compiler caches, and it does not manage several
  toolchains side by side beyond rustup's own behaviour.
- `system-packages` speaks apt only, needs root or sudo, and fails
  loudly on other operating systems rather than approximating a
  package manager mapping.
- Setting the rustup default is on by default; workflows that select
  toolchains per command with the `cargo +<toolchain>` syntax should
  disable it and treat the action as installation only.

## Security model

- Caches carry no signatures. Integrity relies on GitHub's branch
  scoping plus the workflow discipline this suite recommends: restore
  anywhere, save only from the default branch and version tags. The
  actions cannot enforce that discipline; a workflow that saves from
  pull requests accepts the poisoning risk itself.
- Cleanup needs a token with `actions: write`. The action uses the
  token it is given and cannot reduce its scope; give it to the
  cleanup job only.

## Scope

Cargo workspaces only. Other build systems, Docker layer caching and
general-purpose directory caching are outside this suite, as is any
hosted service: everything runs inside the workflow that calls it.
