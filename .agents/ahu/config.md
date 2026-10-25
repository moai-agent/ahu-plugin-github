---
okf_version: '0.2'
type: ahu:project
ahu:
  schema_version: 1
  harness_preferences: [codex]
  model_selection: project-ranked
  catalog_version: "2026-10-19"
  model_rankings:
    codex: [gpt-6-astra]
  telemetry:
    enabled: true
    endpoint: http://127.0.0.1:4318
---

# Development agent policy

Local development uses ahu to coordinate reviewed task branches.
