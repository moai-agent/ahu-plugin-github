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

## Install a package

Install ahu 0.11.0 or a compatible later version. Clone this repository at the
reviewed release tag, then compute the package digest from that checkout. Each
package directory has its own identity and digest:

```sh
ahu plugins digest "$(pwd -P)/package"
ahu plugins digest "$(pwd -P)/tracker/package"
```

Use the selected package's digest as both `revision` and `sha256` in a project
binding. Point `source.repository` to this checkout and set `source.subdirectory`
to `package` or `tracker/package`. Use `kind: local`, the package ID
(`github-cli` or `github-tracker`), version `0.1.0`, compatible Ahu/Host API
versions, and `grants: [execute]`. Keep the binding disabled during review, then
install and enable it through Ahu's lifecycle. Commit the binding and resulting
context lock before invocation.

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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tracker/tests -v
```

Tests use mocked `gh` responses and never contact GitHub or write to real issues.
Live read-only checks are appropriate only for repositories the user is
authorized to inspect. Do not test writes against a real issue.

Keep sources pinned to the reviewed package digest and inspect the security
inventory before enabling either package. Installing a package does not grant
permission to write to GitHub.
