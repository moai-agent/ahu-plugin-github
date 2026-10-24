---
okf_version: "0.2"
type: ahu:plugin
title: GitHub issue tracker
description: Explicit GitHub issue read and progress operations through gh
tags: [github, issues, tracking]
schema_version: 1
id: github-tracker
package: ahu-github-tracker-plugin
host_api: 1
cargo_manifest: Cargo.toml
entrypoint:
  path: bin/entrypoint
  protocol_version: 1
  timeout_ms: 30000
---

# GitHub issue tracker plugin

The separate `github-tracker` package can read issues, add idempotency-marked
progress comments, and close an issue after an explicit stale-state check. Its
task workflow supports `link_task`, `sync_task`, and `sync_replay`: linking stores only a hash
of the Ahu task UUID in an issue comment, and progress sync requires that marker
and posts a revision-keyed lifecycle update. Callers supply the task snapshot
and expected issue `updatedAt`; the plugin does not read Ahu storage or schedule
automatic updates. When Ahu supplies a single read observation, the plugin
takes the task UUID, revision, and lifecycle from that snapshot rather than from
caller data. Replay sync posts bounded, ordered task-service changes and checks
that the final change matches the snapshot. It stops at stale or ambiguous
writes and remains an explicit one-shot call; there is no automatic lifecycle
subscription or persistent retry queue. `task_evidence` can post a bounded, explicitly caller-supplied
progress, review, or result summary to the linked issue; it is labeled
unverified by Ahu and keyed by a digest of task, operation, and body. A
coordinator still has to invoke sync/evidence after a task transition. See the
repository security inventory before enabling `execute`.

Marker reconciliation checks the comment author against the currently
authenticated `gh` account. Use the same GitHub identity when linking and
updating a task; markers posted by other collaborators are treated as
untrusted text and cannot claim idempotent completion or advance a revision.
Caller-supplied comment and evidence bodies containing any reserved Ahu marker
prefix are rejected, so plugin-authored comments remain distinguishable from
caller-provided text. Malformed or out-of-range legacy progress revisions are
ignored during reconciliation.
