# Repository implementation instructions

## Customer runtime language policy

All ahu-maintained customer-facing executable code must be implemented in Rust.
This includes the CLI, shipped harness adapters, executable plugins, session
hosts, and setup or runtime helpers. Installing and using these components must
not require Python. Python is allowed only for development tooling, tests,
fixtures, and experiments that are not shipped as supported customer features.
A Rust launcher that delegates its implementation to Python does not satisfy
this rule.

Skills and declarative configuration remain text. External tools such as `gh`,
cmux, and native harnesses retain their own implementations; this rule governs
code maintained and distributed by ahu. Plugin wire protocols remain independent
of implementation language, but ahu-maintained supported plugins must use Rust.

Existing Python adapters require migration before their next supported release.
Keep them external and optional during migration, preserve their behavior and
security checks, and verify the Rust replacements through protocol tests and
live integration checks. Do not claim existing adapters already meet this policy.
