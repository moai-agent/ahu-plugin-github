import importlib.util
from importlib.machinery import SourceFileLoader
import json
import pathlib
import sys
import io
import tempfile
import unittest

sys.dont_write_bytecode = True
ENTRY = pathlib.Path(__file__).parent / "oracle/entrypoint.py"
spec = importlib.util.spec_from_loader("github_tracker", SourceFileLoader("github_tracker", str(ENTRY)))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.old_json = plugin.gh_json
        self.old_run = plugin.run_gh
        self.old_actor = plugin.current_actor
        plugin.current_actor = lambda: "ahu-bot"

    def tearDown(self):
        plugin.gh_json = self.old_json
        plugin.run_gh = self.old_run
        plugin.current_actor = self.old_actor

    def test_comment_checks_private_visibility_and_posts_fixed_marker(self):
        calls = []
        issue_data = {"number": 9, "state": "OPEN", "updatedAt": "rev-1"}
        written = False

        def fake_json(args, mutation=False):
            calls.append((args, mutation))
            if args[:3] == ["repo", "view", "org/private"]:
                return {"nameWithOwner": "org/private", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return [{"body": "Progress " + plugin.marker("task-1"), "author": "ahu-bot"}] if written else []
            raise AssertionError(args)

        plugin.gh_json = fake_json
        def write(args, mutation=False):
            nonlocal written
            calls.append((args, mutation))
            written = True
            return b""
        plugin.run_gh = write
        result = plugin.write_comment("org/private", 9, "progress", "task-1", "rev-1", "private")
        self.assertEqual(result["effect"], "confirmed")
        write = next(args for args, mutation in calls if mutation)
        self.assertEqual(write[:5], ["issue", "comment", "9", "--repo", "org/private"])
        self.assertIn(plugin.marker("task-1"), write[-1])
        self.assertNotIn("task-1", write[-1])

    def test_comment_is_idempotent_when_marker_exists(self):
        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["issue", "view"]:
                return {"number": 2, "state": "OPEN", "updatedAt": "rev"}
            if args[0] == "api":
                return [{"body": "already " + plugin.marker("op-2"), "author": "ahu-bot"}]
            raise AssertionError(args)

        plugin.gh_json = fake_json
        plugin.run_gh = lambda *_args, **_kwargs: self.fail("must not write a duplicate")
        result = plugin.write_comment("org/repo", 2, "progress", "op-2", "rev", "public")
        self.assertEqual(result["effect"], "already_applied")

    def test_concurrent_duplicate_comment_writes_are_reported_ambiguous(self):
        issue_data = {"number": 2, "state": "OPEN", "updatedAt": "rev"}
        comments = []
        mutations = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(comments)
            raise AssertionError(args)

        def concurrent_write(args, mutation=False):
            self.assertTrue(mutation)
            mutations.append(args)
            tag = plugin.marker("shared-operation")
            # A second invocation passed its own preflight before either write
            # became visible. Reconciliation must not claim one write won.
            comments.extend([
                {"body": "progress\n\n" + tag, "author": "ahu-bot"},
                {"body": "progress\n\n" + tag, "author": "ahu-bot"},
            ])
            return b""

        plugin.gh_json = fake_json
        plugin.run_gh = concurrent_write
        with self.assertRaises(plugin.PluginError) as ambiguous:
            plugin.write_comment("org/repo", 2, "progress", "shared-operation", "rev", "public")

        self.assertEqual(ambiguous.exception.code, "outcome_unknown")
        self.assertEqual(len(mutations), 1)
        self.assertEqual(len(comments), 2)

    def test_comment_listing_uses_bounded_jq_query_and_flattens_pages(self):
        tag = plugin.marker("large-issue-progress")
        calls = []
        pages = (
            json.dumps([{"id": 2, "body": "progress " + tag}]) + "\n"
            + json.dumps([{"id": 3, "body": "retry " + tag}]) + "\n"
        ).encode()

        def fake_run(args, mutation=False):
            calls.append((args, mutation))
            return pages

        plugin.run_gh = fake_run
        rows = plugin.comment_rows("org/repo", 8, tag)
        self.assertEqual([row["id"] for row in rows], [2, 3])
        args, mutation = calls[0]
        self.assertFalse(mutation)
        self.assertIn("--paginate", args)
        self.assertIn("--jq", args)
        self.assertNotIn("--slurp", args)
        query = args[args.index("--jq") + 1]
        self.assertIn(json.dumps(tag), query)
        self.assertIn("{body: .body, author: .user.login}", query)
        self.assertEqual(args[-1], "repos/org/repo/issues/8/comments")

        prefix = "<!-- ahu:task-progress:v1:hashed-task:"
        plugin.comment_rows("org/repo", 8, marker_prefixes=[prefix])
        prefix_args, prefix_mutation = calls[-1]
        self.assertFalse(prefix_mutation)
        prefix_query = prefix_args[prefix_args.index("--jq") + 1]
        self.assertIn("contains(" + json.dumps(prefix) + ")", prefix_query)

    def test_current_actor_uses_gh_identity_and_fails_closed_without_login(self):
        calls = []
        plugin.current_actor = self.old_actor

        def valid(args, mutation=False):
            calls.append(args)
            return {"login": "Ahu-Bot"}

        plugin.gh_json = valid
        self.assertEqual(plugin.current_actor(), "ahu-bot")
        self.assertEqual(calls, [["api", "user"]])
        plugin.gh_json = lambda *_args, **_kwargs: {"id": 123}
        with self.assertRaises(plugin.PluginError) as missing:
            plugin.current_actor()
        self.assertEqual(missing.exception.code, "failed")

    def test_comment_rejects_visibility_mismatch_and_stale_issue(self):
        plugin.gh_json = lambda args, mutation=False: {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
        with self.assertRaises(plugin.PluginError) as denied:
            plugin.write_comment("org/repo", 2, "progress", "op", "rev", "private")
        self.assertEqual(denied.exception.code, "denied")

        def stale(args, mutation=False):
            if args[0] == "api":
                return []
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            return {"number": 2, "state": "OPEN", "updatedAt": "newer"}

        plugin.gh_json = stale
        with self.assertRaises(plugin.PluginError) as conflict:
            plugin.write_comment("org/repo", 2, "progress", "op", "older", "public")
        self.assertEqual(conflict.exception.code, "stale")

    def test_internal_repository_is_rejected_before_mutation(self):
        plugin.gh_json = lambda args, mutation=False: {
            "nameWithOwner": "org/repo", "visibility": "INTERNAL"
        }
        plugin.run_gh = lambda *_args, **_kwargs: self.fail("must not mutate internal repo")
        with self.assertRaises(plugin.PluginError) as denied:
            plugin.close_issue("org/repo", 2, "close-internal", "rev", "public")
        self.assertEqual(denied.exception.code, "denied")

    def test_close_requires_visibility_and_fresh_open_state_then_verifies(self):
        reads = []

        def fake_json(args, mutation=False):
            reads.append(args)
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            return {"number": 2, "state": "CLOSED" if len([x for x in reads if x[:2] == ["issue", "view"]]) > 1 else "OPEN",
                    "updatedAt": "rev"}

        writes = []
        plugin.gh_json = fake_json
        plugin.run_gh = lambda args, mutation=False: writes.append((args, mutation)) or b""
        result = plugin.close_issue("org/repo", 2, "close-1", "rev", "private")
        self.assertEqual(result["effect"], "confirmed")
        self.assertTrue(writes[0][1])
        self.assertEqual(writes[0][0][:3], ["issue", "close", "2"])

    def test_close_refuses_stale_issue_before_mutation(self):
        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PUBLIC"} if args[:2] == ["repo", "view"]
            else {"number": 4, "state": "OPEN", "updatedAt": "changed"})
        plugin.run_gh = lambda *_a, **_k: self.fail("stale close must not be sent")
        with self.assertRaises(plugin.PluginError) as conflict:
            plugin.close_issue("org/repo", 4, "close-2", "old", "public")
        self.assertEqual(conflict.exception.code, "stale")

    def test_only_fixed_actions_and_arguments_are_accepted(self):
        for data in (
            {"action": "api", "path": "repos/org/repo/issues"},
            {"action": "close", "repository": "org/repo", "number": 1, "operation_id": "x",
             "expected_updated_at": "now", "expected_visibility": "private", "force": True},
            {"action": "issue", "repository": "-R/..", "number": 1},
            {"action": "sync_task", "repository": "org/repo", "number": 1,
             "task": "00000000-0000-4000-8000-000000000001", "revision": 1,
             "lifecycle": "deleted", "expected_updated_at": "r1", "expected_visibility": "private"},
        ):
            with self.assertRaises(plugin.PluginError):
                plugin.invoke(data)

    def test_request_parser_rejects_duplicate_keys_and_wrong_grants(self):
        old_stdin = plugin.sys.stdin
        try:
            plugin.sys.stdin = io.TextIOWrapper(io.BytesIO(
                b'{"protocol_version":1,"protocol_version":1,"plugin_id":"github-tracker","grant":{"operation":"invoke"},"context":{"invocation_id":"test"},"input":{}}'))
            with self.assertRaises(ValueError):
                plugin.read_request()
            plugin.sys.stdin = io.TextIOWrapper(io.BytesIO(
                b'{"protocol_version":1,"plugin_id":"github-tracker","grant":{"operation":"other"},"context":{"invocation_id":"test"},"input":{}}'))
            with self.assertRaises(plugin.PluginError):
                plugin.read_request()
        finally:
            plugin.sys.stdin = old_stdin

    def test_repository_and_operation_inputs_are_bounded(self):
        for value in ("-R/repo", "../repo", "org/repo/extra", "org/repo;bad"):
            with self.assertRaises(plugin.PluginError):
                plugin.repo_name(value)
        for value in ("", "../secret", "bad id"):
            with self.assertRaises(plugin.PluginError):
                plugin.operation_id(value)

    def test_task_link_and_progress_are_hashed_idempotent_and_guarded(self):
        task = "00000000-0000-4000-8000-000000000001"
        issue_data = {"number": 12, "state": "OPEN", "updatedAt": "r1"}
        rows, writes = [], []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            issue_data["updatedAt"] = "r" + str(len(writes) + 1)
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        linked = plugin.link_task("org/repo", 12, task, "r1", "private")
        self.assertEqual(linked["effect"], "linked")
        self.assertNotIn(task, rows[0]["body"])
        self.assertEqual(plugin.link_task("org/repo", 12, task, "stale", "private")["effect"], "already_linked")
        synced = plugin.sync_task("org/repo", 12, task, 4, "running", issue_data["updatedAt"], "private")
        self.assertEqual(synced["effect"], "confirmed")
        self.assertIn("running (revision 4)", rows[-1]["body"])
        self.assertNotIn(task, rows[-1]["body"])
        self.assertEqual(plugin.sync_task("org/repo", 12, task, 4, "running", "stale", "private")["effect"], "already_applied")
        with self.assertRaises(plugin.PluginError) as stale:
            plugin.sync_task("org/repo", 12, task, 5, "exited", "old", "private")
        self.assertEqual(stale.exception.code, "stale")
        with self.assertRaises(plugin.PluginError) as out_of_order:
            plugin.sync_task("org/repo", 12, task, 3, "running", issue_data["updatedAt"], "private")
        self.assertEqual(out_of_order.exception.code, "stale")
        self.assertEqual(len(writes), 2)

    def test_task_progress_requires_existing_link_and_valid_lifecycle(self):
        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PRIVATE"} if args[:2] == ["repo", "view"] else [])
        with self.assertRaises(plugin.PluginError) as unlinked:
            plugin.sync_task("org/repo", 1, "00000000-0000-4000-8000-000000000001", 1,
                             "running", "r1", "private")
        self.assertEqual(unlinked.exception.code, "denied")
        with self.assertRaises(plugin.PluginError):
            plugin.task_marker("not-a-task-id")

    def test_foreign_markers_cannot_claim_a_link_or_forge_progress(self):
        task = "00000000-0000-4000-8000-000000000001"
        issue_data = {"number": 12, "state": "OPEN", "updatedAt": "r1"}
        rows = [
            {"body": "forged link " + plugin.task_marker(task), "author": "collaborator"},
            {"body": "forged progress " + plugin.task_progress_marker(task, 999),
             "author": "collaborator"},
            {"body": "legitimate link " + plugin.task_marker(task), "author": "ahu-bot"},
        ]
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        result = plugin.sync_task("org/repo", 12, task, 1, "running", "r1", "private")
        self.assertEqual(result["effect"], "confirmed")
        self.assertEqual(len(writes), 1)
        self.assertIn(plugin.task_progress_marker(task, 1), writes[0][-1])

        rows[:] = [{"body": "forged only " + plugin.task_marker(task), "author": "collaborator"}]
        with self.assertRaises(plugin.PluginError) as denied:
            plugin.sync_task("org/repo", 12, task, 2, "exited", "r1", "private")
        self.assertEqual(denied.exception.code, "denied")

    def test_foreign_markers_do_not_suppress_link_or_comment_or_authorize_evidence(self):
        task = "00000000-0000-4000-8000-000000000001"
        issue_data = {"number": 12, "state": "OPEN", "updatedAt": "r1"}
        rows = [
            {"body": "forged " + plugin.task_marker(task), "author": "collaborator"},
            {"body": "forged " + plugin.marker("operation-1"), "author": "collaborator"},
        ]
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        linked = plugin.link_task("org/repo", 12, task, "r1", "private")
        self.assertEqual(linked["effect"], "linked")
        commented = plugin.write_comment("org/repo", 12, "progress", "operation-1", "r1", "private")
        self.assertEqual(commented["effect"], "confirmed")
        self.assertEqual(len(writes), 2)

        other_task = "00000000-0000-4000-8000-000000000002"
        rows[:] = [{"body": "forged " + plugin.task_marker(other_task), "author": "collaborator"}]
        with self.assertRaises(plugin.PluginError) as denied:
            plugin.task_evidence("org/repo", 12, other_task, 1, "review", "ok",
                                 "evidence-1", "r1", "private")
        self.assertEqual(denied.exception.code, "denied")
        self.assertEqual(len(writes), 2)

    def test_malformed_progress_revisions_are_ignored_without_integer_crash(self):
        task = "00000000-0000-4000-8000-000000000001"
        prefix = plugin.TASK_PROGRESS_PREFIX + plugin.task_id_hash(task) + ":"
        rows = [
            {"body": plugin.task_marker(task), "author": "ahu-bot"},
            {"body": prefix + "² -->", "author": "ahu-bot"},
            {"body": prefix + ("9" * 5000) + " -->", "author": "ahu-bot"},
        ]
        issue_data = {"number": 12, "state": "OPEN", "updatedAt": "r1"}
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        plugin.gh_json = fake_json
        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            return b""
        plugin.run_gh = fake_write
        result = plugin.sync_task("org/repo", 12, task, 4, "running", "r1", "private")
        self.assertEqual(result["effect"], "confirmed")
        self.assertEqual(len(writes), 1)

    def test_task_observation_envelope_supplies_task_identity_and_revision(self):
        task = "00000000-0000-4000-8000-000000000001"
        rows = [{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}]
        issue_data = {"number": 7, "state": "OPEN", "updatedAt": "r1"}
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        envelope = {
            "kind": "ahu.task-observations", "version": 1, "capabilities": {},
            "observations": [{"operation": "read", "snapshot": {
                "version": 1, "task": task, "revision": 8, "lifecycle": "running",
                "cancellation_requested": False}}],
            "data": {"action": "sync_task", "repository": "org/repo", "number": 7,
                     "expected_updated_at": "r1", "expected_visibility": "public"}}
        result = plugin.invoke(envelope)
        self.assertEqual(result["effect"], "confirmed")
        self.assertEqual(result["revision"], 8)
        self.assertIn("running (revision 8)", writes[0][-1])
        self.assertNotIn(task, writes[0][-1])
        envelope["data"] = {"action": "task_evidence", "repository": "org/repo", "number": 7,
                            "task": "spoofed-task", "revision": 999, "kind": "review",
                            "body": "Reviewer accepted the change", "operation_id": "review-1",
                            "expected_updated_at": "r1", "expected_visibility": "public"}
        evidence = plugin.invoke(envelope)
        self.assertEqual(evidence["effect"], "confirmed")
        self.assertEqual(evidence["revision"], 8)
        self.assertIn("revision 8", writes[-1][-1])
        self.assertNotIn(task, writes[-1][-1])
        with self.assertRaises(plugin.PluginError) as missing_data:
            plugin.invoke({k: v for k, v in envelope.items() if k != "data"})
        self.assertEqual(missing_data.exception.code, "invalid_input")

    def test_replay_sync_posts_revisions_and_reconciles_exact_retry(self):
        task = "00000000-0000-4000-8000-000000000001"
        rows = [{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}]
        issue_data = {"number": 7, "state": "OPEN", "updatedAt": "rev-1"}
        writes = []
        changes = [
            {"revision": 2, "kind": "submitted", "lifecycle": "queued", "cancellation_requested": False},
            {"revision": 3, "kind": "effect_observed", "lifecycle": "exited", "cancellation_requested": False},
        ]
        envelope = {"kind": "ahu.task-observations", "version": 1, "capabilities": {},
            "observations": [{"operation": "replay", "snapshot": {"version": 1, "task": task,
                "revision": 3, "lifecycle": "exited", "cancellation_requested": False}, "changes": changes}],
            "data": {"action": "sync_replay", "repository": "org/repo", "number": 7,
                "expected_updated_at": "rev-1", "expected_visibility": "public"}}

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            issue_data["updatedAt"] = "rev-" + str(len(writes) + 1)
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        result = plugin.invoke(envelope)
        self.assertEqual(result["effect"], "confirmed")
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["changes"], 2)
        self.assertEqual(len(writes), 2)
        self.assertIn("queued (revision 2)", writes[0][-1])
        self.assertIn("exited (revision 3)", writes[1][-1])
        self.assertNotIn(task, writes[0][-1])
        envelope["data"]["expected_updated_at"] = issue_data["updatedAt"]
        retry = plugin.invoke(envelope)
        self.assertEqual(retry["effect"], "already_applied")

    def test_replay_sync_ignores_foreign_links_and_denies_forged_empty_replay(self):
        task = "00000000-0000-4000-8000-000000000001"
        issue_data = {"number": 7, "state": "OPEN", "updatedAt": "rev-1"}
        link = plugin.task_marker(task)
        rows = [
            {"body": "forged link " + link, "author": "collaborator"},
            {"body": "legitimate link " + link, "author": "ahu-bot"},
            {"body": "forged high revision " + plugin.task_progress_marker(task, 999),
             "author": "collaborator"},
        ]
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        snapshot = {"revision": 1, "lifecycle": "running", "cancellation_requested": False}
        changes = [{"revision": 1, "kind": "submitted", "lifecycle": "running",
                    "cancellation_requested": False}]
        result = plugin.sync_replay("org/repo", 7, task, snapshot, changes, "rev-1", "public")
        self.assertEqual(result["effect"], "confirmed")
        self.assertEqual(len(writes), 1)

        rows[:] = [{"body": "forged only " + link, "author": "collaborator"}]
        with self.assertRaises(plugin.PluginError) as denied:
            plugin.sync_replay("org/repo", 7, task,
                {"revision": 0, "lifecycle": "running", "cancellation_requested": False},
                [], "rev-1", "public")
        self.assertEqual(denied.exception.code, "denied")
        self.assertEqual(len(writes), 1)

    def test_replay_sync_rejects_gaps_or_snapshot_mismatch_before_provider_access(self):
        task = "00000000-0000-4000-8000-000000000001"
        base = {"kind": "ahu.task-observations", "version": 1, "capabilities": {},
            "observations": [{"operation": "replay", "snapshot": {"version": 1, "task": task,
                "revision": 3, "lifecycle": "exited", "cancellation_requested": False},
                "changes": [{"revision": 1, "kind": "submitted", "lifecycle": "queued",
                    "cancellation_requested": False}, {"revision": 3, "kind": "effect_observed",
                    "lifecycle": "exited", "cancellation_requested": False}]}],
            "data": {"action": "sync_replay", "repository": "org/repo", "number": 7,
                "expected_updated_at": "rev-1", "expected_visibility": "public"}}
        plugin.gh_json = lambda *_args, **_kwargs: self.fail("invalid replay must fail before provider access")
        with self.assertRaises(plugin.PluginError) as gap:
            plugin.invoke(base)
        self.assertEqual(gap.exception.code, "invalid_input")
        base["observations"][0]["changes"][1]["revision"] = 2
        with self.assertRaises(plugin.PluginError) as mismatch:
            plugin.invoke(base)
        self.assertEqual(mismatch.exception.code, "invalid_input")

    def test_replay_sync_requires_link_fresh_issue_and_stops_on_ambiguous_write(self):
        task = "00000000-0000-4000-8000-000000000001"
        envelope = {"kind": "ahu.task-observations", "version": 1, "capabilities": {},
            "observations": [{"operation": "replay", "snapshot": {"version": 1, "task": task,
                "revision": 1, "lifecycle": "running", "cancellation_requested": False},
                "changes": [{"revision": 1, "kind": "submitted", "lifecycle": "running",
                    "cancellation_requested": False}]}],
            "data": {"action": "sync_replay", "repository": "org/repo", "number": 7,
                "expected_updated_at": "rev-1", "expected_visibility": "public"}}
        issue_data = {"number": 7, "state": "OPEN", "updatedAt": "rev-1"}
        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PUBLIC"} if args[:2] == ["repo", "view"]
            else ([{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}] if args[0] == "api"
                  else dict(issue_data)))
        plugin.run_gh = lambda *_a, **_k: self.fail("stale issue must not write")
        envelope["data"]["expected_updated_at"] = "old"
        with self.assertRaises(plugin.PluginError) as stale:
            plugin.invoke(envelope)
        self.assertEqual(stale.exception.code, "stale")
        envelope["data"]["expected_updated_at"] = "rev-1"
        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PUBLIC"} if args[:2] == ["repo", "view"]
            else ([{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}] if args[0] == "api"
                  else dict(issue_data)))
        attempts = []
        def ambiguous(*_a, **_k):
            attempts.append(True)
            raise plugin.PluginError("outcome_unknown")
        plugin.run_gh = ambiguous
        with self.assertRaises(plugin.PluginError) as unknown:
            plugin.invoke(envelope)
        self.assertEqual(unknown.exception.code, "outcome_unknown")
        self.assertEqual(len(attempts), 1)

    def test_task_evidence_is_caller_supplied_bounded_idempotent_and_linked(self):
        task = "00000000-0000-4000-8000-000000000001"
        rows = [{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}]
        issue_data = {"number": 12, "state": "OPEN", "updatedAt": "r1"}
        writes = []

        def fake_json(args, mutation=False):
            if args[:2] == ["repo", "view"]:
                return {"nameWithOwner": "org/repo", "visibility": "PRIVATE"}
            if args[:2] == ["issue", "view"]:
                return dict(issue_data)
            if args[0] == "api":
                return list(rows)
            raise AssertionError(args)

        def fake_write(args, mutation=False):
            writes.append(args)
            rows.append({"body": args[-1], "author": "ahu-bot"})
            issue_data["updatedAt"] = "r" + str(len(writes) + 1)
            return b""

        plugin.gh_json, plugin.run_gh = fake_json, fake_write
        for kind, operation in (("progress", "ev-1"), ("review", "ev-2"), ("result", "ev-3")):
            result = plugin.task_evidence("org/repo", 12, task, 8, kind, "Reviewed summary",
                                           operation, issue_data["updatedAt"], "private")
            self.assertEqual(result["effect"], "confirmed")
            self.assertIn("caller-supplied; not verified by Ahu", rows[-1]["body"])
            self.assertIn(kind + " evidence", rows[-1]["body"])
            self.assertNotIn(task, rows[-1]["body"])
            repeated = plugin.task_evidence("org/repo", 12, task, 8, kind, "Reviewed summary",
                                            operation, "stale", "private")
            self.assertEqual(repeated["effect"], "already_applied")
            conflict = plugin.task_evidence("org/repo", 12, task, 8, kind, "Changed summary",
                                            operation, issue_data["updatedAt"], "private")
            self.assertEqual(conflict["effect"], "conflict")
        self.assertEqual(len(writes), 3)

    def test_task_evidence_denies_unlinked_and_stale_writes_and_bounds_content(self):
        task = "00000000-0000-4000-8000-000000000001"
        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PUBLIC"}
            if args[:2] == ["repo", "view"] else [])
        with self.assertRaises(plugin.PluginError) as unlinked:
            plugin.task_evidence("org/repo", 1, task, 1, "review", "ok", "ev-1", "r1", "public")
        self.assertEqual(unlinked.exception.code, "denied")
        with self.assertRaises(plugin.PluginError) as oversized:
            plugin.task_evidence("org/repo", 1, task, 1, "result", "x" * 8001, "ev-2", "r1", "public")
        self.assertEqual(oversized.exception.code, "invalid_input")
        with self.assertRaises(plugin.PluginError) as forged_evidence:
            plugin.task_evidence("org/repo", 1, task, 1, "review",
                "note " + plugin.task_marker(task), "ev-2b", "r1", "public")
        self.assertEqual(forged_evidence.exception.code, "invalid_input")
        with self.assertRaises(plugin.PluginError) as forged_comment:
            plugin.write_comment("org/repo", 1, "note " + plugin.task_marker(task),
                                 "op-2b", "r1", "public")
        self.assertEqual(forged_comment.exception.code, "invalid_input")

        plugin.gh_json = lambda args, mutation=False: (
            {"nameWithOwner": "org/repo", "visibility": "PUBLIC"} if args[:2] == ["repo", "view"]
            else ([{"body": "linked " + plugin.task_marker(task), "author": "ahu-bot"}] if args[0] == "api"
                  else {"number": 1, "state": "OPEN", "updatedAt": "new"}))
        plugin.run_gh = lambda *_a, **_k: self.fail("stale evidence must not write")
        with self.assertRaises(plugin.PluginError) as stale:
            plugin.task_evidence("org/repo", 1, task, 1, "review", "ok", "ev-3", "old", "public")
        self.assertEqual(stale.exception.code, "stale")

    def test_task_actions_require_exact_task_observation_envelope(self):
        task = "00000000-0000-4000-8000-000000000001"
        with self.assertRaises(plugin.PluginError):
            plugin.invoke({"action": "link_task", "repository": "org/repo", "number": 1,
                           "task": task, "expected_updated_at": "r1", "expected_visibility": "private"})

    def test_issue_search_uses_fixed_argv_and_bounds_the_query(self):
        calls = []
        plugin.gh_json = lambda args, mutation=False: calls.append(args) or []
        result = plugin.invoke({"action": "search", "repository": "org/repo", "query": "label:bug", "state": "open"})
        self.assertEqual(result["issues"], [])
        self.assertEqual(calls[0][0:8], ["issue", "list", "--repo", "org/repo", "--state", "open", "--search", "label:bug"])
        with self.assertRaises(plugin.PluginError):
            plugin.invoke({"action": "search", "repository": "org/repo", "query": "x" * 257, "state": "open"})

    def test_ambiguous_write_is_successfully_reported_for_reconciliation(self):
        old_stdout, old_argv = plugin.sys.stdout, plugin.sys.argv
        old_read, old_invoke = plugin.read_request, plugin.invoke
        output = io.StringIO()
        plugin.sys.stdout = output
        plugin.sys.argv = ["entrypoint", "--ahu-plugin-protocol=1"]
        plugin.read_request = lambda: {}
        plugin.invoke = lambda _: (_ for _ in ()).throw(plugin.PluginError("outcome_unknown"))
        try:
            self.assertEqual(plugin.main(), 0)
        finally:
            plugin.sys.stdout, plugin.sys.argv = old_stdout, old_argv
            plugin.read_request, plugin.invoke = old_read, old_invoke
        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"]["effect"], "outcome_unknown")
        self.assertTrue(response["result"]["requires_reconciliation"])

    def test_null_repository_name_returns_protocol_error_before_write(self):
        old_stdout, old_argv = plugin.sys.stdout, plugin.sys.argv
        old_read, old_json = plugin.read_request, plugin.gh_json
        output = io.StringIO()
        plugin.sys.stdout = output
        plugin.sys.argv = ["entrypoint", "--ahu-plugin-protocol=1"]
        plugin.read_request = lambda: {
            "action": "close", "repository": "org/repo", "number": 1,
            "operation_id": "op-1", "expected_updated_at": "rev-1",
            "expected_visibility": "private",
        }
        plugin.gh_json = lambda *_args, **_kwargs: {
            "nameWithOwner": None, "visibility": "PRIVATE",
        }
        try:
            self.assertEqual(plugin.main(), 0)
        finally:
            plugin.sys.stdout, plugin.sys.argv = old_stdout, old_argv
            plugin.read_request, plugin.gh_json = old_read, old_json
        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "failed")

    def test_cli_failure_after_write_process_starts_is_uncertain(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = pathlib.Path(temp) / "gh"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o700)
            old_find, old_timeout = plugin.find_gh, plugin.TIMEOUT
            plugin.find_gh = lambda: str(fake)
            plugin.TIMEOUT = 1.0
            try:
                with self.assertRaises(plugin.PluginError) as mutation:
                    plugin.run_gh(["issue", "comment", "1"], mutation=True)
                self.assertEqual(mutation.exception.code, "outcome_unknown")
                with self.assertRaises(plugin.PluginError) as read:
                    plugin.run_gh(["issue", "view", "1"], mutation=False)
                self.assertEqual(read.exception.code, "failed")
            finally:
                plugin.find_gh, plugin.TIMEOUT = old_find, old_timeout

    def test_rate_limit_missing_scope_and_removed_auth_fail_closed_without_disclosure(self):
        for diagnostic in (
            "API rate limit exceeded",
            "Resource not accessible by integration",
            "You are not logged into any GitHub hosts",
        ):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as temp:
                fake = pathlib.Path(temp) / "gh"
                fake.write_text("#!/bin/sh\necho " + repr(diagnostic) + " >&2\nexit 1\n")
                fake.chmod(0o700)
                old_find, old_timeout = plugin.find_gh, plugin.TIMEOUT
                plugin.find_gh = lambda: str(fake)
                plugin.TIMEOUT = 1.0
                try:
                    with self.assertRaises(plugin.PluginError) as read:
                        plugin.run_gh(["api", "user"], mutation=False)
                    self.assertEqual(read.exception.code, "failed")
                    self.assertNotIn(diagnostic, str(read.exception))
                    with self.assertRaises(plugin.PluginError) as write:
                        plugin.run_gh(["issue", "comment", "1"], mutation=True)
                    self.assertEqual(write.exception.code, "outcome_unknown")
                    self.assertNotIn(diagnostic, str(write.exception))
                finally:
                    plugin.find_gh, plugin.TIMEOUT = old_find, old_timeout

    def test_cli_timeout_kills_fake_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = pathlib.Path(temp) / "gh"
            fake.write_text("#!/bin/sh\nsleep 5\n")
            fake.chmod(0o700)
            old_find, old_timeout = plugin.find_gh, plugin.TIMEOUT
            plugin.find_gh = lambda: str(fake)
            plugin.TIMEOUT = 0.05
            try:
                with self.assertRaises(plugin.PluginError) as timeout:
                    plugin.run_gh(["issue", "close", "1"], mutation=True)
                self.assertEqual(timeout.exception.code, "outcome_unknown")
            finally:
                plugin.find_gh, plugin.TIMEOUT = old_find, old_timeout


if __name__ == "__main__":
    unittest.main()
