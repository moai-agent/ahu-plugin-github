# 0.1.1

The GitHub CLI and tracker packages now contain standalone Rust executables.
The wire protocol and supported operations are unchanged. Runtime requires
`gh` and its native authentication, but no Python. Python remains development
fixture/oracle tooling only.

The explicit packaging command uses CARGO_TARGET_DIR and includes the license,
README and security inventory. Build from the locked source on each target
platform, then pin the staged package digest. Source directories are not
installable executable packages. See README.md for upgrading existing bindings.
