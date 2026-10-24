import importlib.util
from importlib.machinery import SourceFileLoader
import json
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ENTRY = pathlib.Path(__file__).parents[1] / "package/bin/entrypoint"
spec = importlib.util.spec_from_loader("github_entrypoint", SourceFileLoader("github_entrypoint", str(ENTRY)))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.binary = pathlib.Path(self.temp.name) / "gh"
        self.binary.write_text("#!/bin/sh\nprintf '%s' '{}\\n'\n")
        self.binary.chmod(0o700)
        self.original = plugin.find_gh
        plugin.find_gh = lambda: str(self.binary)

    def tearDown(self):
        plugin.find_gh = self.original
        self.temp.cleanup()

    def test_repository_uses_fixed_read_only_argv_and_projects_fields(self):
        self.binary.write_text(
            "#!/bin/sh\n"
            "test \"$1 $2\" = 'repo view' || exit 8\n"
            "printf '%s' '{\"nameWithOwner\":\"org/repo\",\"url\":\"https://github.com/org/repo\",\"defaultBranchRef\":{\"name\":\"main\"},\"secret\":\"ignored\"}'\n"
        )
        self.binary.chmod(0o700)
        result = plugin.invoke({"action": "repository", "repository": "org/repo"})
        self.assertEqual(result["repository"], "org/repo")
        self.assertNotIn("secret", result)

    def test_issue_and_release_actions_use_fixed_read_only_commands(self):
        self.binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = issue ]; then\n"
            "  test \"$2 $3 $4 $5 $6\" = 'view 7 --repo org/repo --json' || exit 8\n"
            "  printf '%s' '{\"number\":7,\"title\":\"sample\",\"state\":\"OPEN\",\"url\":\"https://example.invalid/7\",\"updatedAt\":\"now\"}'\n"
            "else\n"
            "  test \"$1 $2 $3 $4\" = 'release list --repo org/repo' || exit 9\n"
            "  printf '%s' '[{\"tagName\":\"v1\",\"name\":\"one\",\"isLatest\":true,\"publishedAt\":\"now\"}]'\n"
            "fi\n"
        )
        self.binary.chmod(0o700)
        issue = plugin.invoke({"action": "issue", "repository": "org/repo", "number": 7})
        self.assertEqual(issue["issue"]["number"], 7)
        release = plugin.invoke({"action": "releases", "repository": "org/repo"})
        self.assertEqual(release["releases"][0]["tag"], "v1")

    def test_issue_number_is_an_integer_and_repo_is_validated(self):
        with self.assertRaises(plugin.PluginError):
            plugin.invoke({"action": "issue", "repository": "org/repo", "number": True})
        for name in ("org/repo;touch-bad", "../repo", "org/repo/extra", "org/repo\n"):
            with self.assertRaises(plugin.PluginError):
                plugin.invoke({"action": "repository", "repository": name})

    def test_unknown_actions_and_extra_fields_are_rejected(self):
        with self.assertRaises(plugin.PluginError):
            plugin.invoke({"action": "graphql", "query": "mutation { deleteRepository }"})
        with self.assertRaises(plugin.PluginError):
            plugin.invoke({"action": "repository", "repository": "org/repo", "token": "secret"})

    def test_request_parser_rejects_duplicate_keys(self):
        import io
        old_stdin = plugin.sys.stdin
        plugin.sys.stdin = io.TextIOWrapper(io.BytesIO(
            b'{"protocol_version":1,"plugin_id":"github-cli","grant":{"operation":"invoke"},"context":{"invocation_id":"test"},"input":{"action":"repository","action":"issue"}}'
        ))
        try:
            with self.assertRaises(ValueError):
                plugin.read_request()
        finally:
            plugin.sys.stdin = old_stdin

    def test_repository_rejects_option_like_names(self):
        with self.assertRaises(plugin.PluginError):
            plugin.repository_name("-delete/repo")


if __name__ == "__main__":
    unittest.main()
