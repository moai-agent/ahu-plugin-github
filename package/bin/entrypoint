#!/usr/bin/python3
"""Read-only GitHub CLI adapter for Ahu's one-shot plugin protocol."""

import json
import os
import pwd
import selectors
import signal
import subprocess
import sys
import time

MAX_INPUT = 64 * 1024
MAX_OUTPUT = 64 * 1024
TIMEOUT = 12.0
PLUGIN_ID = "github-cli"
REPOSITORY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


class PluginError(Exception):
    pass


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def fail(code="failed"):
    sys.stdout.write(json.dumps({"protocol_version": 1, "status": "error", "code": code}, separators=(",", ":")))
    sys.stdout.flush()
    return 0


def read_request():
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise PluginError
    request = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(request, dict) or set(request) != {"protocol_version", "plugin_id", "grant", "context", "input"}:
        raise PluginError
    if type(request["protocol_version"]) is not int or request["protocol_version"] != 1 or request["plugin_id"] != PLUGIN_ID:
        raise PluginError
    if request["grant"] != {"operation": "invoke"}:
        raise PluginError
    context = request["context"]
    if not isinstance(context, dict) or set(context) != {"invocation_id"}:
        raise PluginError
    if not isinstance(context["invocation_id"], str) or not context["invocation_id"].isascii() or not (1 <= len(context["invocation_id"]) <= 64):
        raise PluginError
    return request["input"]


def home_directory():
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        raise PluginError from None
    if not home.startswith("/"):
        raise PluginError
    return home


def find_gh():
    home = home_directory()
    candidates = [
        "/opt/homebrew/bin/gh",
        "/usr/local/bin/gh",
        "/usr/bin/gh",
        "/bin/gh",
        "/home/linuxbrew/.linuxbrew/bin/gh",
        os.path.join(home, ".local/bin/gh"),
        os.path.join(home, "bin/gh"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise PluginError


def run_gh(args):
    try:
        child = subprocess.Popen(
            [find_gh(), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": home_directory()},
            start_new_session=True,
        )
    except OSError:
        raise PluginError from None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginError
            if not selector.select(remaining):
                raise PluginError
            chunk = os.read(child.stdout.fileno(), 8192)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                raise PluginError
        if child.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise PluginError
    except (OSError, subprocess.TimeoutExpired):
        raise PluginError from None
    finally:
        selector.close()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        child.stdout.close()
    try:
        return json.loads(output, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise PluginError from None


def repository_name(value):
    if not isinstance(value, str) or len(value) > 200 or value.count("/") != 1:
        raise PluginError
    owner, name = value.split("/", 1)
    for part in (owner, name):
        if not part or part.startswith("-") or part in (".", "..") or any(char not in REPOSITORY_CHARS for char in part):
            raise PluginError
    return value


def invoke(data):
    if not isinstance(data, dict) or not isinstance(data.get("action"), str):
        raise PluginError
    action = data["action"]
    if action == "repository" and set(data) == {"action", "repository"}:
        repository = repository_name(data["repository"])
        value = run_gh(["repo", "view", repository, "--json", "nameWithOwner,url,defaultBranchRef"])
        if not isinstance(value, dict):
            raise PluginError
        branch = value.get("defaultBranchRef")
        return {
            "provider": PLUGIN_ID,
            "action": action,
            "repository": value.get("nameWithOwner") if isinstance(value.get("nameWithOwner"), str) else repository,
            "url": value.get("url") if isinstance(value.get("url"), str) else None,
            "default_branch": branch.get("name") if isinstance(branch, dict) and isinstance(branch.get("name"), str) else None,
        }
    if action == "issue" and set(data) == {"action", "repository", "number"}:
        repository = repository_name(data["repository"])
        number = data["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise PluginError
        value = run_gh(["issue", "view", str(number), "--repo", repository, "--json", "number,title,state,url,updatedAt"])
        if not isinstance(value, dict) or value.get("number") != number:
            raise PluginError
        return {
            "provider": PLUGIN_ID,
            "action": action,
            "issue": {
                "number": number,
                "title": value.get("title") if isinstance(value.get("title"), str) else "",
                "state": value.get("state") if isinstance(value.get("state"), str) else "",
                "url": value.get("url") if isinstance(value.get("url"), str) else None,
                "updated_at": value.get("updatedAt") if isinstance(value.get("updatedAt"), str) else None,
            },
        }
    if action == "releases" and set(data) == {"action", "repository"}:
        repository = repository_name(data["repository"])
        value = run_gh(["release", "list", "--repo", repository, "--limit", "20", "--json", "tagName,name,isLatest,publishedAt"])
        if not isinstance(value, list):
            raise PluginError
        rows = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("tagName"), str):
                raise PluginError
            rows.append({
                "tag": item["tagName"],
                "name": item.get("name") if isinstance(item.get("name"), str) else "",
                "latest": item.get("isLatest") is True,
                "published_at": item.get("publishedAt") if isinstance(item.get("publishedAt"), str) else None,
            })
        return {"provider": PLUGIN_ID, "action": action, "releases": rows}
    raise PluginError


def main():
    if sys.argv[1:] != ["--ahu-plugin-protocol=1"]:
        return fail("invalid_input")
    try:
        result = invoke(read_request())
        raw = json.dumps({"protocol_version": 1, "status": "ok", "result": result}, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_OUTPUT:
            return fail("failed")
        sys.stdout.write(raw)
        sys.stdout.flush()
    except (PluginError, ValueError, TypeError, UnicodeError, RecursionError):
        return fail("failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
