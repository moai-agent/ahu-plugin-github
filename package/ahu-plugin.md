---
okf_version: "0.2"
type: ahu:plugin
title: GitHub CLI example
description: Read-only GitHub inspection through the Ahu plugin protocol
tags: [github, issues, releases, example]
schema_version: 1
id: github-cli
package: ahu-github-cli-plugin
host_api: 1
cargo_manifest: Cargo.toml
entrypoint:
  path: bin/entrypoint
  protocol_version: 1
  timeout_ms: 15000
---

# GitHub CLI example plugin

Read-only Ahu one-shot plugin. See the repository README for supported actions
and local setup.
