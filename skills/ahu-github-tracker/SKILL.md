---
name: ahu-github-tracker
description: Use GitHub's installed gh CLI to inspect and update issues when the project explicitly selects the GitHub tracker workflow.
---

# GitHub issue tracker

Use the user's installed and authenticated `gh` CLI for GitHub issue work. Ahu
does not broker GitHub credentials or implement GitHub issue operations in its
core. This skill is optional project guidance; use it only when the project
chooses GitHub as its tracker and the task calls for tracker work. Check
`gh auth status` if authentication is uncertain, and never print, copy, or
request tokens.

Use `gh issue` commands for ordinary tracker work. Prefer structured JSON
output for reads, inspect the target issue before writing, and preserve its
existing labels, assignees, milestones, projects, and body unless the requested
change specifically requires editing them. Do not create a task or issue merely
to record your own work unless asked.

## Inspect before changing

Resolve the repository from the user's request or the current project context.
Do not guess a repository or issue number. Check repository visibility before
posting content:

```sh
gh repo view OWNER/REPO --json nameWithOwner,visibility,url
gh issue view ISSUE --repo OWNER/REPO --json number,title,state,body,labels,assignees,milestone,url,updatedAt
```

Treat public issue comments as public. Keep secrets, credentials, local paths,
private roadmap details, and unrelated user data out of comments. For private
issues, still include only information appropriate for that issue's readers.

When reviewing a set of issues, use `gh issue list` with explicit repository,
state, and fields; check pagination when the result may exceed one page. Read
each issue's acceptance criteria before deciding whether it is complete.

## Comment, edit, or close

Before a write, confirm that the user requested or authorized that specific
change. For issue comments, use `gh issue comment ISSUE --repo OWNER/REPO
--body-file PATH` with a temporary file outside the repository when the text is
multiline. Report what changed and the validation actually performed. Do not
claim uncommitted local code is available remotely.

Use `gh issue edit` only for fields the task explicitly asks to change. Avoid
replacing an entire issue body when a focused comment or field edit is enough.

Close an issue only after checking its acceptance criteria and required
delivery steps. A completed agent task alone is not sufficient. If work is
partial or blocked, comment with evidence and remaining work, and leave the
issue open. Verify every write by reading the issue again:

```sh
gh issue view ISSUE --repo OWNER/REPO --json state,updatedAt,labels,url
gh issue view ISSUE --repo OWNER/REPO --comments
```

Never retry an ambiguous write blindly. Read the issue and comments first to
determine whether the original operation took effect.

## Ahu coordination

When assigned Ahu work, use Ahu's task record as the source for task state and
the repository's documented Git workflow. A Git worktree is a collaboration
boundary, not a security sandbox. Do not represent task lifecycle as an
automatic GitHub issue lifecycle. If the project has an optional GitHub plugin
installed and explicitly asks for a structured plugin action, follow that
plugin's own instructions; otherwise use `gh` directly.

Do not put tracker credentials, private issue content, or private roadmap
references in commits, repository documentation, or agent prompts that do not
need them.
