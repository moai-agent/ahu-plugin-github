# Security inventory

This repository contains two separately identified Ahu packages. They share a
source repository but receive separate package digests and project bindings.
The read-only `github-cli` package is under `package/`; the state-changing
`github-tracker` package is under `tracker/package/`. Both are same-user
executables, not operating-system sandboxes. Ahu runs either only after the
project binding is committed, enabled, and granted `execute`.

## `github-cli` (read-only)

| Capability | Effect | Boundary and control |
| --- | --- | --- |
| Repository lookup | Reads repository name, URL, and default branch | Fixed `gh repo view` fields; validated `owner/name` |
| Issue lookup | Reads one issue's title, state, URL, and update time | Fixed `gh issue view` fields and positive numeric issue number |
| Release listing | Reads at most 20 releases | Fixed `gh release list` fields |

It does not read auth status or configuration, and receives no credentials from
Ahu. `gh` uses its normal local authentication store. The package sends no input
to a shell and discards CLI diagnostics. Its live network/auth behavior depends
on the user's `gh` setup.

## `github-tracker` (read and write)

| Capability | Effect | Boundary and control |
| --- | --- | --- |
| Issue search, listing, and lookup | Reads at most 50 issues per search/list or one issue | Fixed subcommands/fields; bounded query; validated repository and number |
| Progress comment | Posts a body plus an operation marker | Requires a caller-provided operation ID, exact expected issue update time, and expected repository visibility; checks for the marker before writing and verifies it afterward |
| Issue close | Closes one open issue | Requires operation ID, expected update time, and expected `private`/`public` repository visibility; rereads state after mutation |
| Task link | Posts one link marker for an Ahu task on a caller-selected issue | Requires an exact single read observation from a task-observations envelope; stores only the SHA-256 digest of the task ID; refuses duplicates and requires expected update time and visibility |
| Task progress sync | Posts a revision-keyed progress comment for a linked issue | Requires an existing link marker; validates lifecycle values; rejects out-of-order revisions and stale issue state; never closes or reopens issues |
| Task replay sync | Posts bounded lifecycle changes from one Ahu replay observation | Requires an existing link marker and fresh open issue; validates ordered revisions and final snapshot; stops on stale or ambiguous writes; does not retry automatically |
| Task evidence | Posts caller-supplied progress, review, or result summary for a linked task | Requires one exact read observation and an existing link; caps content at 8,000 UTF-8 bytes; hashes task ID, operation ID, and body in the marker; labels evidence as unverified by Ahu; stale-checks before writing |
| Authentication | `gh` uses the local auth store for the requested repo | Token material is not read by plugin code, passed from Ahu, logged, or returned |

Writes are never automatic: a caller must explicitly invoke the selected package
with an action and `execute` grant. Comment bodies may be published to GitHub;
the caller must ensure the content is appropriate for the repository's
visibility. Every write action dynamically compares repository visibility with
the caller's expected value. The comment operation marker is an HTML comment in
the issue body and is visible in raw Markdown. Task link, progress, and evidence
markers hold only SHA-256 digests of the Ahu task ID; evidence markers also hash
the operation ID and content. The raw ID, task prompts, and execution history
are never written. Evidence text is caller-supplied, is not validated against
the Ahu task result or reviewer decision, and is explicitly labeled unverified.
The plugin keeps no local task-to-issue state; the association exists only in
the issue's marker comments.

Comment markers contain only a SHA-256 digest of the caller's operation ID, so
the raw correlation value is not copied into the issue body. This does not make
the comment content private.

Marker reconciliation applies a fixed `gh api --paginate --jq` filter to each
page and sends only matching comments to the plugin process. This avoids
buffering unrelated comment bodies in the plugin's bounded output; `gh` still
retrieves the provider pages, and an unusually large number of matching markers
can still produce an explicit bounded-output failure.

The stale timestamp check is not an atomic GitHub compare-and-swap. A concurrent
remote edit can race the check and a close request. The plugin does not retry a
write; replay sync stops at the first ambiguous result, and if `gh` times out,
exits ambiguously, or post-write verification fails,
the caller receives `outcome_unknown` and must inspect the provider before any
new invocation. The marker avoids sequential duplicate comments, but concurrent
identical invocations can still race and duplicate a comment. Task link and
progress markers also make sequential retries idempotent, but concurrent
invocations can race and duplicate comments; revision checks do not provide an
atomic compare-and-swap. Issue closure is state-idempotent but does not prove
that this invocation caused the closed state.

Both packages bound input/output, reject duplicate JSON keys and unknown action
fields, use fixed executable paths and argv, discard stderr, and kill timed-out
child process groups. They cannot prevent same-user code from using other local
permissions. Tests use fake CLI responses and never contact GitHub or write to a
real issue. Ahu currently offers foreground one-shot calls; a caller may
explicitly pass read or replay task observations to a single invocation. The
plugin does not subscribe to lifecycle changes or persist a pending-sync queue,
and it cannot atomically transact with GitHub. GitHub Projects V2 is also not
implemented.
