# Ahu GitHub CLI plugins

This repository contains two separately pinned Ahu one-shot plugins backed by
the installed GitHub CLI (`gh`). The `github-cli` package is read-only and can
inspect repositories, issues, and recent releases. The `github-tracker` package
can list/read issues, post progress comments, close issues with explicit
preconditions, link an Ahu task to an issue, and post revision-keyed task
progress. Review [SECURITY.md](SECURITY.md) before granting either package.

Both adapters use fixed, validated argument forms, project only selected fields,
discard CLI diagnostics, and use the user's normal local `gh` authentication.
They do not accept shell fragments, arbitrary API paths, or GraphQL queries.
Because Ahu starts executables with a restricted environment, each locates `gh`
in common system/Homebrew paths and resolves `HOME` through the OS account
database. Neither package receives credentials from Ahu. The tracker receives
task context only through an explicitly supplied task-observations envelope and
persists only a SHA-256 digest of the Ahu task ID in issue markers.


## Build and install a package

Both customer executables are standalone Rust binaries with no ahu-core or
Python runtime dependency. Rust sources share a small local adapter crate;
`package/` and `tracker/package/` contain source manifests, not installable
packages. Python exists only under tests as a development oracle and fake CLI.
Native `gh` and its normal local authentication remain runtime prerequisites.
Supported build platforms are Unix (macOS and Linux); artifacts are specific to
the build platform and architecture.

Build from a reviewed checkout using Rust 1.96 or later and the committed lockfile.
The explicit Rust packaging command creates a **new** destination containing
`github-cli/` and `github-tracker/`, each with `bin/entrypoint`, a Cargo identity
manifest, and `ahu-plugin.md`. It never installs or enables plugins. No build
runs implicitly during plugin installation, and the packages contain no Python,
source tree, or test driver. Use an external directory, or a new directory under
ignored `dist/`; the destination's parent must already exist.

```sh
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo run --locked --bin package -- /tmp/ahu-github-dist
github_package_root="$(cd /tmp/ahu-github-dist && pwd -P)"
ahu plugins digest "$github_package_root/github-cli"
ahu plugins digest "$github_package_root/github-tracker"
```

Compute package digests **after** building. Use canonical paths: Ahu rejects
symlinked path components (including `/tmp` on macOS). `SHA256SUMS` in the destination root
records executable hashes for inspection; these are not Ahu package digests.
Build results and package digests depend on the toolchain, platform and source;
this command is reproducible procedure, not a cross-platform byte-identity claim.

Install ahu 0.11.0 or a compatible later version. Use each resulting package's
digest as both `revision` and `sha256` in its project binding. Set
`source.repository` to the absolute destination directory and
`source.subdirectory` to `github-cli` or `github-tracker`. Use `kind: local`, the
corresponding package ID, version `0.1.0`, compatible Ahu/Host API versions, and
`grants: [execute]`. Keep the binding disabled during review, then install and
enable it through Ahu's lifecycle. Commit the binding and context lock before
invocation. Generated Cargo manifests are package identity metadata; they are
not installation-time build recipes.

Fixture validation of the Rust migration is automated below. A local check on
2026-10-25 with gh 2.100.0 installed the pinned Rust packages through ahu,
read repository and issue-list data, and posted an authorized progress update
with the tracker adapter. The update was verified independently. Other account
configurations and live write actions still require their own evidence; fixture
tests cover the broader operation and failure matrix.

The read-only package supports `repository`, `issue`, and `releases` actions.
The tracker package supports `search`, `issues`, `issue`, `comment`, `close`,
`link_task`, `sync_task`, `sync_replay`, and `task_evidence` actions.
The companion `skills/ahu-github-tracker/SKILL.md` is the primary agent
workflow: it assumes the agent can use the user's installed `gh` CLI and keeps
tracker policy in project skills rather than Ahu's Rust core. Review the skill,
then copy its directory into the consuming project's `.agents/skills/`
directory if you want agents to discover it:

```sh
mkdir -p /path/to/project/.agents/skills
cp -R skills/ahu-github-tracker /path/to/project/.agents/skills/
```

The skill is optional guidance; it does not install or enable the plugin,
authorize provider writes, or make GitHub a core dependency. Agents can use
`gh` directly without installing either executable package. The packages are
optional structured adapters for projects that prefer Ahu's plugin invocation
contract; Ahu itself neither probes `gh` nor forwards GitHub credentials.
For tracker writes, pass `expected_updated_at` from a fresh issue read,
`expected_visibility` (`private` or `public`), and a stable operation ID. A
comment appends an HTML marker containing only the SHA-256 digest of its operation
ID to the Markdown body. Closure verifies
the issue state afterward. These checks reduce mistakes and sequential
duplicates, but they cannot make provider writes atomic against concurrent edits.
An ambiguous result requires a manual GitHub read before any repeat invocation.

The `link_task` action posts a link marker holding only the SHA-256 digest of the
Ahu task ID on a caller-selected issue and is idempotent per issue. The
`sync_task` action posts revision-keyed progress comments for an already linked
issue and rejects stale issue state and out-of-order revisions. Both derive the
task ID, revision, and lifecycle from the single exact read observation of a
`ahu.task-observations` envelope passed to the invocation; the envelope's data
payload still supplies the action and its arguments. Sync is explicit and
per-invocation: terminal task states never close or reopen issues, and closing
remains a separate explicit `close` action.

The `task_evidence` action attaches an explicitly supplied `progress`, `review`,
or `result` summary to the linked issue. Ahu supplies only task identity and
revision; the evidence text is caller-provided and the plugin labels it as
unverified. Each operation ID is idempotent for the same task, type, and body;
reusing it with different content returns `conflict`. Comments are capped at
8,000 UTF-8 bytes and include a digest marker, never the raw task UUID or
operation ID. The caller must review the summary for sensitive content before
posting.

The `sync_replay` action consumes one Ahu `replay` observation and posts each
bounded, ordered lifecycle revision as an idempotency-marked comment for an
already-linked issue. It verifies that the final replay change agrees with the
snapshot, refreshes the issue update time between writes, and stops on stale
state or an ambiguous write. Reinvoke with a fresh replay and issue timestamp
to reconcile; the plugin does not automatically retry or store a pending queue.

Example read-only request:

```json
{"action":"repository","repository":"owner/repo"}
```

Example tracker comment request:

```json
{"action":"comment","repository":"owner/repo","number":123,"body":"Progress update","operation_id":"work-123-progress-1","expected_updated_at":"<value from the latest issue read>","expected_visibility":"private"}
```

The comment body is sent to GitHub and may be public. Do not include secrets or
private project metadata in content for a public repository. Ahu's invocation
model is a foreground one-shot call: lifecycle sync happens only when a caller
explicitly invokes `sync_task` or `sync_replay` with task observations. It does
not provide automatic lifecycle subscription, durable pending-sync storage,
GitHub Projects V2, or atomic stale-write protection.

## Validate locally

```sh
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo fmt --all --check
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo clippy --workspace --all-targets --locked -- -D warnings
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo clippy --no-default-features --all-targets --locked -- -D warnings
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo test --workspace --locked
CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo build --workspace --features test-driver --locked
PYTHONDONTWRITEBYTECODE=1 CARGO_TARGET_DIR=/tmp/ahu-rust-github-target python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tracker/tests -v
```

The `test-driver` feature builds a development-only executable that accepts a
fake provider path and timeout. Production entrypoints never consult these
fixture settings, and packaging explicitly builds without this feature.
Independent wire/process tests invoke compiled executables, use synthetic
provider state, and cover all actions, framing, marker authorship, pagination,
mutation ambiguity, deadlines and descendant cleanup. Original Python unit tests
remain useful oracle coverage; passing them alone does not validate Rust.
Tests never contact GitHub or change live issues or cmux sessions. Coordinator
live checks must use authorized repositories and reviewed write procedures.

Keep sources pinned to the reviewed package digest and inspect the security
inventory before enabling either package. Installing a package does not grant
permission to write to GitHub.
