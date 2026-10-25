#!/usr/bin/python3
"""Explicit GitHub issue reads and writes for Ahu's one-shot plugin API."""

import json
import hashlib
import os
import pwd
import selectors
import signal
import subprocess
import sys
import time

MAX_INPUT = 64 * 1024
MAX_OUTPUT = 64 * 1024
TIMEOUT = 20.0
INVOCATION_TIMEOUT = 26.0
INVOCATION_DEADLINE = None
PLUGIN_ID = "github-tracker"
REPO_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
OP_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
MARKER_PREFIX = "<!-- ahu-operation:"
TASK_LINK_PREFIX = "<!-- ahu-task-link:v1:"
TASK_PROGRESS_PREFIX = "<!-- ahu-task-progress:v1:"
TASK_EVIDENCE_PREFIX = "<!-- ahu-task-evidence:v1:"
RESERVED_MARKER_PREFIXES = (MARKER_PREFIX, TASK_LINK_PREFIX, TASK_PROGRESS_PREFIX, TASK_EVIDENCE_PREFIX)


class PluginError(Exception):
    def __init__(self, code="failed"):
        super().__init__(code)
        self.code = code


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def read_request():
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise PluginError("invalid_input")
    request = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(request, dict) or set(request) != {"protocol_version", "plugin_id", "grant", "context", "input"}:
        raise PluginError("invalid_input")
    if type(request["protocol_version"]) is not int or request["protocol_version"] != 1 or request["plugin_id"] != PLUGIN_ID:
        raise PluginError("invalid_input")
    if request["grant"] != {"operation": "invoke"}:
        raise PluginError("invalid_input")
    context = request["context"]
    if not isinstance(context, dict) or set(context) != {"invocation_id"} or not isinstance(context["invocation_id"], str):
        raise PluginError("invalid_input")
    return request["input"]


def home_directory():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        raise PluginError("unavailable") from None


def find_gh():
    home = home_directory()
    for path in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh", "/bin/gh",
                 "/home/linuxbrew/.linuxbrew/bin/gh", os.path.join(home, ".local/bin/gh"),
                 os.path.join(home, "bin/gh")):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise PluginError("unavailable")


def run_gh(args, mutation=False):
    remaining = TIMEOUT
    if INVOCATION_DEADLINE is not None:
        remaining = min(remaining, INVOCATION_DEADLINE - time.monotonic())
    if remaining <= 0:
        raise PluginError("outcome_unknown" if mutation else "failed")
    try:
        child = subprocess.Popen([find_gh(), *args], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/bin:/bin", "HOME": home_directory()},
                                 start_new_session=True)
    except (OSError, PluginError):
        raise PluginError("unavailable") from None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + remaining
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise PluginError("outcome_unknown" if mutation else "failed")
            chunk = os.read(child.stdout.fileno(), 8192)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                raise PluginError("outcome_unknown" if mutation else "failed")
        if child.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise PluginError("outcome_unknown" if mutation else "failed")
    except (OSError, subprocess.TimeoutExpired):
        raise PluginError("outcome_unknown" if mutation else "failed") from None
    finally:
        selector.close()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        child.stdout.close()
    return bytes(output)


def gh_json(args, mutation=False):
    raw = run_gh(args, mutation)
    try:
        if "--paginate" in args:
            # gh emits one jq result per page when --paginate is combined with
            # --jq. The comment query filters each page before these bounded
            # results reach this process.
            text = raw.decode("utf-8")
            decoder = json.JSONDecoder(object_pairs_hook=unique_object)
            values = []
            offset = 0
            while True:
                while offset < len(text) and text[offset].isspace():
                    offset += 1
                if offset == len(text):
                    break
                page, offset = decoder.raw_decode(text, offset)
                if not isinstance(page, list):
                    raise ValueError("invalid paginated result")
                values.extend(page)
            return values
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise PluginError("outcome_unknown" if mutation else "failed") from None


def repo_name(value):
    if not isinstance(value, str) or len(value) > 200 or value.count("/") != 1:
        raise PluginError("invalid_input")
    owner, name = value.split("/", 1)
    for part in (owner, name):
        if not part or part.startswith("-") or part in (".", "..") or any(c not in REPO_CHARS for c in part):
            raise PluginError("invalid_input")
    return value


def issue_number(value):
    if type(value) is not int or value < 1 or value > 2**31 - 1:
        raise PluginError("invalid_input")
    return value


def operation_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(c not in OP_CHARS for c in value):
        raise PluginError("invalid_input")
    return value


def visibility(repo, expected):
    if expected not in ("private", "public"):
        raise PluginError("invalid_input")
    value = gh_json(["repo", "view", repo, "--json", "nameWithOwner,visibility"])
    name = value.get("nameWithOwner") if isinstance(value, dict) else None
    if not isinstance(name, str) or name.lower() != repo.lower():
        raise PluginError("failed")
    actual = value.get("visibility", "").lower() if isinstance(value, dict) else None
    if actual not in ("private", "public"):
        raise PluginError("denied")
    if actual != expected:
        raise PluginError("denied")
    return actual


def issue(repo, number):
    value = gh_json(["issue", "view", str(number), "--repo", repo,
                     "--json", "number,title,state,url,updatedAt"])
    if not isinstance(value, dict) or value.get("number") != number:
        raise PluginError("failed")
    return value


def comment_rows(repo, number, markers=None, marker_prefixes=None):
    # Filter each API page in gh so the plugin does not buffer every issue
    # comment just to find its own operation markers. Marker strings and
    # prefixes are JSON-encoded literals, never jq source.
    if isinstance(markers, str):
        markers = [markers]
    markers = [] if markers is None else markers
    marker_prefixes = [] if marker_prefixes is None else marker_prefixes
    if (not isinstance(markers, list) or not isinstance(marker_prefixes, list)
            or not markers and not marker_prefixes
            or any(not isinstance(marker, str) or not marker for marker in markers)
            or any(not isinstance(prefix, str) or not prefix for prefix in marker_prefixes)):
        raise PluginError("invalid_input")
    conditions = [
        f"($body | contains({json.dumps(marker)}))" for marker in markers
    ] + [
        f"($body | contains({json.dumps(prefix)}))" for prefix in marker_prefixes
    ]
    conditions = " or ".join(conditions)
    query = f"[.[] | select((.body | strings) as $body | ({conditions})) | {body_author_projection()}]"
    value = gh_json(["api", "--paginate", "--jq", query,
                     f"repos/{repo}/issues/{number}/comments"])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise PluginError("failed")
    return value


def body_author_projection():
    # Preserve the GitHub-authenticated author alongside marker-bearing text.
    # Comment bodies are user-controlled, so a marker alone is not proof that
    # this installation performed the operation.
    return "{body: .body, author: .user.login}"


def current_actor():
    value = gh_json(["api", "user"])
    if not isinstance(value, dict) or not isinstance(value.get("login"), str) or not value["login"]:
        raise PluginError("failed")
    return value["login"].casefold()


def authored_by(rows, actor):
    return [row for row in rows if isinstance(row.get("author"), str)
            and row["author"].casefold() == actor]


def marker(operation):
    digest = hashlib.sha256(operation.encode("utf-8")).hexdigest()
    return MARKER_PREFIX + "v1:" + digest + " -->"


def contains_reserved_marker(text):
    return any(prefix in text for prefix in RESERVED_MARKER_PREFIXES)


def task_id_hash(task):
    if (not isinstance(task, str) or len(task) != 36
            or [i for i, c in enumerate(task) if c == "-"] != [8, 13, 18, 23]
            or any(c not in "0123456789abcdef-" for c in task.lower())):
        raise PluginError("invalid_input")
    return hashlib.sha256(task.lower().encode("ascii")).hexdigest()


def task_marker(task):
    return TASK_LINK_PREFIX + task_id_hash(task) + " -->"


def task_progress_marker(task, revision):
    return TASK_PROGRESS_PREFIX + task_id_hash(task) + ":" + str(revision) + " -->"


def task_evidence_marker(task, kind, operation, body):
    operation_hash = hashlib.sha256(operation.encode("utf-8")).hexdigest()
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (TASK_EVIDENCE_PREFIX + task_id_hash(task) + ":" + kind + ":"
            + operation_hash + ":" + body_hash + " -->")


def link_task(repo, number, task, expected_updated_at, expected_visibility):
    visibility(repo, expected_visibility)
    current = issue(repo, number)
    tag = task_marker(task)
    rows = comment_rows(repo, number, [tag])
    actor = current_actor()
    linked = [row for row in authored_by(rows, actor) if tag in (row.get("body") or "")]
    if len(linked) == 1:
        return {"effect": "already_linked", "issue": number}
    if len(linked) > 1:
        raise PluginError("outcome_unknown")
    if current.get("state") != "OPEN" or current.get("updatedAt") != expected_updated_at:
        raise PluginError("stale")
    run_gh(["issue", "comment", str(number), "--repo", repo,
            "--body", "Ahu task linked.\n\n" + tag], mutation=True)
    verified = [row for row in authored_by(comment_rows(repo, number, [tag]), actor)
                if tag in (row.get("body") or "")]
    if len(verified) != 1:
        raise PluginError("outcome_unknown")
    return {"effect": "linked", "issue": number}


def sync_task(repo, number, task, revision, lifecycle, expected_updated_at, expected_visibility):
    if type(revision) is not int or revision < 0 or revision > 2**64 - 1 or lifecycle not in (
            "prepared", "queued", "starting", "running", "waiting_for_approval",
            "outcome_unknown", "exited", "failed", "cancelled"):
        raise PluginError("invalid_input")
    visibility(repo, expected_visibility)
    link = task_marker(task)
    progress_prefix = TASK_PROGRESS_PREFIX + task_id_hash(task) + ":"
    rows = comment_rows(repo, number, [link], [progress_prefix])
    actor = current_actor()
    rows = authored_by(rows, actor)
    links = [row for row in rows if link in (row.get("body") or "")]
    if not links:
        raise PluginError("denied")
    if len(links) != 1:
        raise PluginError("outcome_unknown")
    tag = task_progress_marker(task, revision)
    prior = [row for row in rows if tag in (row.get("body") or "")]
    if prior:
        return {"effect": "already_applied", "issue": number, "revision": revision}
    revisions = []
    prefix = TASK_PROGRESS_PREFIX + task_id_hash(task) + ":"
    for row in rows:
        body = row.get("body") or ""
        for line in body.splitlines():
            if line.startswith(prefix) and line.endswith(" -->"):
                raw = line[len(prefix):-4]
                if raw.isascii() and raw.isdigit() and len(raw) <= 20:
                    parsed = int(raw)
                    if parsed <= 2**64 - 1:
                        revisions.append(parsed)
    if revisions and revision < max(revisions):
        raise PluginError("stale")
    current = issue(repo, number)
    if current.get("state") != "OPEN" or current.get("updatedAt") != expected_updated_at:
        raise PluginError("stale")
    text = "Ahu task status: " + lifecycle + " (revision " + str(revision) + ").\n\n" + tag
    run_gh(["issue", "comment", str(number), "--repo", repo, "--body", text], mutation=True)
    verified = [row for row in authored_by(comment_rows(repo, number, [tag]), actor)
                if tag in (row.get("body") or "")]
    if len(verified) != 1:
        raise PluginError("outcome_unknown")
    return {"effect": "confirmed", "issue": number, "revision": revision}


def sync_replay(repo, number, task, snapshot, changes, expected_updated_at, expected_visibility):
    if (not isinstance(snapshot, dict)
            or type(snapshot.get("revision")) is not int or snapshot["revision"] < 0
            or snapshot["revision"] > 2**64 - 1
            or snapshot.get("lifecycle") not in (
                "prepared", "queued", "starting", "running", "waiting_for_approval",
                "outcome_unknown", "exited", "failed", "cancelled")
            or type(snapshot.get("cancellation_requested")) is not bool
            or not isinstance(changes, list) or len(changes) > 128):
        raise PluginError("invalid_input")
    prior_revision = None
    for change in changes:
        if (not isinstance(change, dict)
                or set(change) != {"revision", "kind", "lifecycle", "cancellation_requested"}
                or type(change["revision"]) is not int or not 1 <= change["revision"] <= 2**64 - 1
                or (prior_revision is not None and change["revision"] != prior_revision + 1)
                or change["kind"] not in ("admitted", "submitted", "cancellation_requested",
                    "input_accepted", "effect_claimed", "effect_observed", "reconciled")
                or change["lifecycle"] not in (
                    "prepared", "queued", "starting", "running", "waiting_for_approval",
                    "outcome_unknown", "exited", "failed", "cancelled")
                or type(change["cancellation_requested"]) is not bool):
            raise PluginError("invalid_input")
        prior_revision = change["revision"]
    if changes and (changes[-1]["revision"] != snapshot["revision"]
            or changes[-1]["lifecycle"] != snapshot["lifecycle"]
            or changes[-1]["cancellation_requested"] != snapshot["cancellation_requested"]):
        raise PluginError("invalid_input")

    visibility(repo, expected_visibility)
    link = task_marker(task)
    progress_prefix = TASK_PROGRESS_PREFIX + task_id_hash(task) + ":"
    rows = comment_rows(repo, number, [link], [progress_prefix])
    actor = current_actor()
    rows = authored_by(rows, actor)
    links = [row for row in rows if link in (row.get("body") or "")]
    if not links:
        raise PluginError("denied")
    if len(links) != 1:
        raise PluginError("outcome_unknown")
    current = issue(repo, number)
    if (current.get("state") != "OPEN" or not isinstance(current.get("updatedAt"), str)
            or current.get("updatedAt") != expected_updated_at):
        raise PluginError("stale")

    effects = []
    for change in changes:
        result = sync_task(repo, number, task, change["revision"], change["lifecycle"],
                           current.get("updatedAt"), expected_visibility)
        effects.append(result["effect"])
        current = issue(repo, number)
        if current.get("state") != "OPEN" or not isinstance(current.get("updatedAt"), str):
            raise PluginError("stale")
    return {"effect": "confirmed" if "confirmed" in effects else "already_applied",
            "issue": number, "revision": snapshot["revision"], "changes": len(changes)}


def task_evidence(repo, number, task, revision, kind, body, operation,
                  expected_updated_at, expected_visibility):
    if (type(revision) is not int or revision < 0 or revision > 2**64 - 1
            or kind not in ("progress", "review", "result")
            or not isinstance(body, str) or not body.strip()
            or len(body.encode("utf-8")) > 8000
            or contains_reserved_marker(body)
            or any(ord(c) < 32 and c not in "\n\r\t" for c in body)):
        raise PluginError("invalid_input")
    operation = operation_id(operation)
    tag = task_evidence_marker(task, kind, operation, body)
    visibility(repo, expected_visibility)
    evidence_prefix = (TASK_EVIDENCE_PREFIX + task_id_hash(task) + ":" + kind + ":"
                       + hashlib.sha256(operation.encode("utf-8")).hexdigest() + ":")
    rows = comment_rows(repo, number, [task_marker(task)], [evidence_prefix])
    actor = current_actor()
    rows = authored_by(rows, actor)
    links = [row for row in rows if task_marker(task) in (row.get("body") or "")]
    if not links:
        raise PluginError("denied")
    if len(links) != 1:
        raise PluginError("outcome_unknown")
    matches = [row for row in rows if any(
        line.startswith(evidence_prefix) and line.endswith(" -->")
        for line in (row.get("body") or "").splitlines())]
    exact = [row for row in matches if tag in (row.get("body") or "")]
    if len(exact) == 1 and len(matches) == 1:
        return {"effect": "already_applied", "issue": number, "revision": revision}
    if matches:
        return {"effect": "conflict", "issue": number, "revision": revision}
    current = issue(repo, number)
    if current.get("state") != "OPEN" or current.get("updatedAt") != expected_updated_at:
        raise PluginError("stale")
    text = ("Ahu task " + kind + " evidence (caller-supplied; not verified by Ahu, revision "
            + str(revision) + "):\n\n" + body.rstrip() + "\n\n" + tag)
    run_gh(["issue", "comment", str(number), "--repo", repo, "--body", text], mutation=True)
    verified = [row for row in authored_by(comment_rows(repo, number, [tag]), actor)
                if tag in (row.get("body") or "")]
    if len(verified) != 1:
        raise PluginError("outcome_unknown")
    return {"effect": "confirmed", "issue": number, "revision": revision}


def write_comment(repo, number, body, operation, expected_updated_at, expected_visibility):
    visibility(repo, expected_visibility)
    if (not body.strip() or contains_reserved_marker(body)
            or len((body.rstrip() + "\n\n" + marker(operation)).encode("utf-8")) > 16000):
        raise PluginError("invalid_input")
    tag = marker(operation)
    actor = current_actor()
    prior = [row for row in authored_by(comment_rows(repo, number, tag), actor)
             if tag in (row.get("body") or "")]
    if prior:
        if len(prior) != 1:
            raise PluginError("outcome_unknown")
        return {"operation_id": operation, "effect": "already_applied", "issue": number}
    current = issue(repo, number)
    if current.get("state") != "OPEN" or current.get("updatedAt") != expected_updated_at:
        raise PluginError("stale")
    text = body.rstrip() + "\n\n" + tag
    run_gh(["issue", "comment", str(number), "--repo", repo, "--body", text], mutation=True)
    verified = [row for row in authored_by(comment_rows(repo, number, tag), actor)
                if tag in (row.get("body") or "")]
    if len(verified) != 1:
        raise PluginError("outcome_unknown")
    return {"operation_id": operation, "effect": "confirmed", "issue": number}


def close_issue(repo, number, operation, expected_updated_at, expected_visibility):
    visibility(repo, expected_visibility)
    current = issue(repo, number)
    if current.get("state") == "CLOSED":
        return {"operation_id": operation, "effect": "already_applied", "issue": number, "state": "CLOSED"}
    if current.get("state") != "OPEN" or current.get("updatedAt") != expected_updated_at:
        raise PluginError("stale")
    # gh exposes no atomic compare-and-close operation. This precondition catches
    # prior edits, but concurrent remote edits can still race the close request.
    run_gh(["issue", "close", str(number), "--repo", repo, "--reason", "completed"], mutation=True)
    after = issue(repo, number)
    if after.get("state") != "CLOSED":
        raise PluginError("outcome_unknown")
    return {"operation_id": operation, "effect": "confirmed", "issue": number, "state": "CLOSED"}


def invoke(data):
    observed_task = False
    observed_replay = False
    if isinstance(data, dict) and data.get("kind") == "ahu.task-observations":
        envelope_keys = {"kind", "version", "capabilities", "observations"}
        if (set(data) not in (envelope_keys, envelope_keys | {"data"})
                or type(data["version"]) is not int or data["version"] != 1):
            raise PluginError("invalid_input")
        observations, payload = data["observations"], data.get("data")
        if (not isinstance(observations, list) or len(observations) != 1
                or not isinstance(observations[0], dict)):
            raise PluginError("invalid_input")
        observation = observations[0]
        replay = observation.get("operation") == "replay"
        expected_observation_keys = {"operation", "snapshot", "changes"} if replay else {"operation", "snapshot"}
        if (set(observation) != expected_observation_keys
                or observation.get("operation") not in ("read", "replay")):
            raise PluginError("invalid_input")
        snapshot = observation["snapshot"]
        if (not isinstance(snapshot, dict)
                or set(snapshot) != {"version", "task", "revision", "lifecycle", "cancellation_requested"}
                or type(snapshot["version"]) is not int or snapshot["version"] != 1):
            raise PluginError("invalid_input")
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            raise PluginError("invalid_input")
        payload = dict(payload)
        payload["task"] = snapshot["task"]
        if replay and payload["action"] != "sync_replay":
            raise PluginError("invalid_input")
        if not replay and payload["action"] == "sync_replay":
            raise PluginError("invalid_input")
        if payload["action"] in ("sync_task", "task_evidence"):
            payload["revision"] = snapshot["revision"]
            if payload["action"] == "sync_task":
                payload["lifecycle"] = snapshot["lifecycle"]
        if replay:
            payload["snapshot"] = snapshot
            payload["changes"] = observation["changes"]
        data = payload
        observed_task = True
        observed_replay = replay
    if not isinstance(data, dict) or not isinstance(data.get("action"), str):
        raise PluginError("invalid_input")
    action = data["action"]
    if action == "issue" and set(data) == {"action", "repository", "number"}:
        repo, number = repo_name(data["repository"]), issue_number(data["number"])
        value = issue(repo, number)
        return {"provider": PLUGIN_ID, "action": action, "issue": {
            "number": number, "title": value.get("title", ""), "state": value.get("state", ""),
            "url": value.get("url"), "updated_at": value.get("updatedAt")}}
    if action == "issues" and set(data) == {"action", "repository", "state"}:
        repo = repo_name(data["repository"])
        state = data["state"]
        if state not in ("open", "closed", "all"):
            raise PluginError("invalid_input")
        rows = gh_json(["issue", "list", "--repo", repo, "--state", state,
                        "--limit", "50", "--json", "number,title,state,url,updatedAt"])
        if not isinstance(rows, list):
            raise PluginError("failed")
        return {"provider": PLUGIN_ID, "action": action, "issues": rows}
    if action == "search" and set(data) == {"action", "repository", "query", "state"}:
        repo = repo_name(data["repository"])
        query = data["query"]
        state = data["state"]
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or any(ord(c) < 32 for c in query):
            raise PluginError("invalid_input")
        if state not in ("open", "closed", "all"):
            raise PluginError("invalid_input")
        rows = gh_json(["issue", "list", "--repo", repo, "--state", state,
                        "--search", query, "--limit", "50", "--json",
                        "number,title,state,url,updatedAt"])
        if not isinstance(rows, list):
            raise PluginError("failed")
        return {"provider": PLUGIN_ID, "action": action, "issues": rows}
    if action == "comment" and set(data) == {"action", "repository", "number", "body", "operation_id", "expected_updated_at", "expected_visibility"}:
        repo = repo_name(data["repository"])
        number = issue_number(data["number"])
        if not isinstance(data["body"], str) or not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = write_comment(repo, number, data["body"], operation_id(data["operation_id"]), data["expected_updated_at"], data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    if action == "close" and set(data) == {"action", "repository", "number", "operation_id", "expected_updated_at", "expected_visibility"}:
        repo = repo_name(data["repository"])
        number = issue_number(data["number"])
        if not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = close_issue(repo, number, operation_id(data["operation_id"]), data["expected_updated_at"], data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    if observed_task and action == "link_task" and set(data) == {"action", "repository", "number", "task", "expected_updated_at", "expected_visibility"}:
        repo, number = repo_name(data["repository"]), issue_number(data["number"])
        if not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = link_task(repo, number, data["task"], data["expected_updated_at"], data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    if observed_task and action == "sync_task" and set(data) == {"action", "repository", "number", "task", "revision", "lifecycle", "expected_updated_at", "expected_visibility"}:
        repo, number = repo_name(data["repository"]), issue_number(data["number"])
        if not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = sync_task(repo, number, data["task"], data["revision"], data["lifecycle"],
                           data["expected_updated_at"], data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    if observed_replay and action == "sync_replay" and set(data) == {
            "action", "repository", "number", "task", "snapshot", "changes",
            "expected_updated_at", "expected_visibility"}:
        repo, number = repo_name(data["repository"]), issue_number(data["number"])
        if not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = sync_replay(repo, number, data["task"], data["snapshot"], data["changes"],
                             data["expected_updated_at"], data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    if observed_task and action == "task_evidence" and set(data) == {
            "action", "repository", "number", "task", "revision", "kind", "body",
            "operation_id", "expected_updated_at", "expected_visibility"}:
        repo, number = repo_name(data["repository"]), issue_number(data["number"])
        if not isinstance(data["expected_updated_at"], str):
            raise PluginError("invalid_input")
        result = task_evidence(repo, number, data["task"], data["revision"], data["kind"],
                               data["body"], data["operation_id"], data["expected_updated_at"],
                               data["expected_visibility"])
        return {"provider": PLUGIN_ID, "action": action, **result}
    raise PluginError("invalid_input")


def main():
    global INVOCATION_DEADLINE
    if sys.argv[1:] != ["--ahu-plugin-protocol=1"]:
        return 2
    try:
        request = read_request()
        INVOCATION_DEADLINE = time.monotonic() + INVOCATION_TIMEOUT
        result = invoke(request)
        raw = json.dumps({"protocol_version": 1, "status": "ok", "result": result}, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_OUTPUT:
            return 5
        sys.stdout.write(raw)
        sys.stdout.flush()
    except PluginError as error:
        if error.code == "outcome_unknown":
            result = {"provider": PLUGIN_ID, "effect": "outcome_unknown", "requires_reconciliation": True}
            sys.stdout.write(json.dumps({"protocol_version": 1, "status": "ok", "result": result}, separators=(",", ":")))
        else:
            code = "invalid_input" if error.code == "invalid_input" else "unsupported" if error.code in ("stale", "denied", "unavailable") else "failed"
            sys.stdout.write(json.dumps({"protocol_version": 1, "status": "error", "code": code}, separators=(",", ":")))
    except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError, KeyError):
        sys.stdout.write('{"protocol_version":1,"status":"error","code":"invalid_input"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
