"""Independent process/wire regressions against compiled Rust executables.

Build: CARGO_TARGET_DIR=/tmp/ahu-rust-github-target cargo build --workspace --features test-driver
Run: python3 -m unittest discover -s tests -p test_wire.py -v
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = pathlib.Path(os.environ.get('CARGO_TARGET_DIR', '/tmp/ahu-rust-github-target')) / 'debug'
TASK = '00000000-0000-4000-8000-000000000001'
HASH = hashlib.sha256(TASK.encode()).hexdigest()
LINK = '<!-- ahu-task-link:v1:' + HASH + ' -->'
PROGRESS = '<!-- ahu-task-progress:v1:' + HASH + ':'
OP = '<!-- ahu-operation:v1:' + hashlib.sha256(b'op').hexdigest() + ' -->'

def frame(data, tracker=True):
    return {'protocol_version': 1, 'plugin_id': 'github-tracker' if tracker else 'github-cli',
            'grant': {'operation': 'invoke'}, 'context': {'invocation_id': 'test'}, 'input': data}

def action(name, **extra):
    return dict(action=name, repository='org/repo', number=7, expected_updated_at='r1', expected_visibility='public', **extra)

def observed(data, revision=4, changes=None):
    snapshot = dict(version=1, task=TASK, revision=revision, lifecycle='running', cancellation_requested=False)
    observation = dict(operation='read', snapshot=snapshot)
    if changes is not None:
        observation.update(operation='replay', changes=changes)
    return dict(kind='ahu.task-observations', version=1, capabilities={}, observations=[observation], data=data)

class WireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name)
        self.gh = self.home / 'gh'
        self.gh.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/support/fake_gh.py').read_text())
        self.gh.chmod(0o700)
        self.state = {'rows': [], 'issue': {'number': 7, 'title': 'sample', 'state': 'OPEN', 'updatedAt': 'r1'}}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        (self.home / 'state.json').write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads((self.home / 'state.json').read_text())
        return self.state

    def calls(self):
        path = self.home / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def invoke(self, data=None, tracker=True, raw=None, timeout=5000):
        env = dict(os.environ, FIXTURE_GH=str(self.gh), FIXTURE_HOME=str(self.home), FIXTURE_TRACKER='1' if tracker else '0', FIXTURE_TIMEOUT=str(timeout))
        response = subprocess.run([str(TARGET / 'wire-driver'), '--ahu-plugin-protocol=1'], input=raw if raw is not None else json.dumps(frame(data, tracker)).encode(), capture_output=True, env=env, timeout=8)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(response.stderr, b'')
        self.assertLessEqual(len(response.stdout), 65536)
        return json.loads(response.stdout)

    def effect(self, data, effect, **kwargs):
        response = self.invoke(data, **kwargs)
        self.assertEqual(response['status'], 'ok', response)
        self.assertEqual(response['result']['effect'], effect, response)
        return response['result']

    def test_read_operations_and_fixed_arguments(self):
        for name in ['repository', 'issue', 'releases']:
            data = dict(action=name, repository='org/repo')
            if name == 'issue': data['number'] = 7
            result = self.invoke(data, tracker=False)
            self.assertEqual(result['status'], 'ok', result)
            self.assertNotIn('secret', json.dumps(result))
        for name in ['issue', 'issues', 'search']:
            data = dict(action=name, repository='org/repo')
            if name == 'issue': data['number'] = 7
            else: data['state'] = 'open'
            if name == 'search': data['query'] = 'label:bug'
            self.assertEqual(self.invoke(data)['status'], 'ok')

    def test_invalid_wire_refuses_provider_access(self):
        valid = frame({'action': 'issue', 'repository': 'org/repo', 'number': 7})
        raws = [b'{}', b'[]', b'null', b'\xff', b'{' , b' ' * 65537,
                json.dumps(valid).replace('"number": 7', '"number": 7, "number": 8').encode(),
                json.dumps(valid).replace('"protocol_version": 1', '"protocol_version": true').encode(),
                json.dumps(valid).replace('"invoke"', '"other"').encode(),
                json.dumps(valid).encode() + b'{}',
                b'[' * 200 + b']' * 200]
        for raw in raws:
            with self.subTest(raw=raw[:80]): self.assertEqual(self.invoke(raw=raw)['status'], 'error')
        self.assertEqual(self.calls(), [])

    def test_invalid_actions_and_bounds_refuse_provider_access(self):
        cases = [dict(action='api', path='anything'), dict(action='issue', repository='-x/repo', number=7),
                 dict(action='issue', repository='../repo', number=7), dict(action='issue', repository='org/repo;bad', number=7),
                 dict(action='issue', repository='org/repo', number=True), dict(action='issue', repository='org/repo', number=2**31),
                 dict(action='search', repository='org/repo', state='open', query='x'*257),
                 action('close', operation_id='bad id'), action('close', operation_id='op', force=True),
                 action('link_task', task=TASK), observed(action('sync_task'), revision=True)]
        for data in cases:
            self.assertEqual(self.invoke(data)['status'], 'error', data)
        self.assertEqual(self.calls(), [])

    def test_comment_close_idempotency_stale_visibility(self):
        self.effect(action('comment', operation_id='op', body='progress'), 'confirmed')
        self.effect(action('comment', operation_id='op', body='progress'), 'already_applied')
        self.assertEqual(self.load()['writes'], 1)
        self.assertIn(OP, self.state['rows'][0]['body'])
        self.assertEqual(self.invoke(action('close', operation_id='close'))['status'], 'error')
        self.effect(dict(action('close', operation_id='close'), expected_updated_at='r2'), 'confirmed')
        self.effect(action('close', operation_id='close'), 'already_applied')
        for visibility in ['PRIVATE', 'INTERNAL', None]:
            self.state['visibility'] = visibility; self.save()
            self.assertEqual(self.invoke(action('close', operation_id='close'))['status'], 'error')

    def test_link_sync_author_checks_revision_and_observation_override(self):
        self.state['rows'] = [{'body': LINK, 'author': 'collaborator'}, {'body': PROGRESS+'999 -->', 'author': 'collaborator'}]; self.save()
        self.effect(observed(action('link_task')), 'linked')
        self.effect(observed(action('link_task')), 'already_linked')
        data = dict(action('sync_task'), expected_updated_at='r2', task='spoofed', revision=999, lifecycle='bad')
        self.effect(observed(data), 'confirmed')
        self.effect(observed(data), 'already_applied')
        self.assertEqual(self.invoke(observed(dict(data, expected_updated_at='r3'), revision=3))['status'], 'error')
        rows = self.load()['rows']; self.assertNotIn(TASK, json.dumps(rows))
        self.assertIn('running (revision 4)', rows[-1]['body'])
        self.assertEqual(self.state['writes'], 2)

    def test_marker_line_boundaries_preserve_stale_and_conflict_guards(self):
        for separator in ['\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029']:
            with self.subTest(separator=repr(separator)):
                self.state = {'rows': [{'body': LINK, 'author': 'ahu-bot'},
                                       {'body': 'note'+separator+PROGRESS+'9 -->', 'author': 'ahu-bot'}],
                              'issue': {'number': 7, 'title': 'sample', 'state': 'OPEN', 'updatedAt': 'r1'}}
                self.save()
                result = self.invoke(observed(action('sync_task'), revision=8))
                self.assertEqual(result['status'], 'error', result)
                self.assertFalse(self.load().get('writes'))
                prefix = '<!-- ahu-task-evidence:v1:'+HASH+':review:'+hashlib.sha256(b'op').hexdigest()+':'
                marker = prefix+hashlib.sha256(b'previous body').hexdigest()+' -->'
                self.state['rows'][1]['body'] = 'note'+separator+marker
                self.save()
                self.effect(observed(action('task_evidence', kind='review', body='different body', operation_id='op')), 'conflict')
                self.assertFalse(self.load().get('writes'))

    def test_foreign_links_deny_sync_evidence_and_empty_replay(self):
        self.state['rows'] = [{'body': LINK, 'author': 'collaborator'}]; self.save()
        for data in [observed(action('sync_task')), observed(action('task_evidence', kind='review', body='ok', operation_id='ev')), observed(action('sync_replay'), changes=[])]:
            self.assertEqual(self.invoke(data)['status'], 'error')
        self.assertFalse(self.load().get('writes'))

    def test_foreign_operation_cannot_suppress_comment(self):
        self.state['rows'] = [{'body': OP, 'author': 'collaborator'}]; self.save()
        self.effect(action('comment', body='ok', operation_id='op'), 'confirmed')

    def test_evidence_types_idempotency_conflicts_and_content(self):
        self.state['rows'] = [{'body': LINK, 'author': 'ahu-bot'}]; self.save()
        for i, kind in enumerate(['progress', 'review', 'result'], 1):
            data = observed(dict(action('task_evidence', kind=kind, body='Reviewed summary', operation_id=kind), expected_updated_at='r'+str(i)))
            self.effect(data, 'confirmed')
            self.effect(data, 'already_applied')
            data['data']['body'] = 'changed'
            self.effect(data, 'conflict')
        self.assertEqual(self.load()['writes'], 3)
        for row in self.state['rows'][1:]: self.assertIn('caller-supplied; not verified by Ahu', row['body'])
        self.assertNotIn(TASK, json.dumps(self.state['rows']))

    def test_reserved_markers_and_evidence_bounds(self):
        for body in [LINK, OP, PROGRESS, '<!-- ahu-task-evidence:v1:']:
            for data in [action('comment', body=body, operation_id='op'), observed(action('task_evidence', kind='review', body=body, operation_id='op'))]:
                self.assertEqual(self.invoke(data)['status'], 'error')
        for body in ['x'*8001, 'é'*4001, '\x00', '   ']:
            self.assertEqual(self.invoke(observed(action('task_evidence', kind='review', body=body, operation_id='op')))['status'], 'error')
        self.assertEqual(self.calls(), [])

    def test_malformed_legacy_revisions_are_ignored(self):
        self.state['rows'] = [{'body': LINK, 'author': 'ahu-bot'}] + [{'body': PROGRESS+v+' -->', 'author': 'ahu-bot'} for v in ['²', '9'*5000, str(2**64), '-1']]; self.save()
        self.effect(observed(action('sync_task')), 'confirmed')

    def test_duplicate_markers_are_ambiguous(self):
        for tag, data in [(OP, action('comment', body='ok', operation_id='op')), (LINK, observed(action('link_task'))), (PROGRESS+'4 -->', observed(action('sync_task')))]:
            self.state['rows'] = [{'body': LINK, 'author': 'ahu-bot'}] if tag.startswith(PROGRESS) else []
            self.state['rows'] += [{'body': tag, 'author': 'ahu-bot'}]*2; self.save()
            self.effect(data, 'outcome_unknown')

    def test_replay_order_final_snapshot_refresh_and_retry(self):
        self.state['rows'] = [{'body': LINK, 'author': 'ahu-bot'}]; self.save()
        changes = [dict(revision=r, kind='submitted', lifecycle='running', cancellation_requested=False) for r in [3,4]]
        data = observed(action('sync_replay'), changes=changes)
        result = self.effect(data, 'confirmed'); self.assertEqual(result['changes'], 2)
        data['data']['expected_updated_at'] = 'r3'
        self.effect(data, 'already_applied')
        self.assertEqual(self.load()['writes'], 2)
        for changes in [[dict(changes[0], revision=2), changes[1]], [changes[0]], [dict(changes[1], revision=True)]]:
            bad = observed(action('sync_replay'), changes=changes)
            before = len(self.calls()); self.assertEqual(self.invoke(bad)['status'], 'error'); self.assertEqual(len(self.calls()), before)

    def test_post_dispatch_failures_always_require_reconciliation(self):
        for name in ['comment', 'close', 'link_task', 'sync_task', 'task_evidence', 'sync_replay']:
            for mode in ['fail_write', 'fail_verify', 'duplicate_write', 'oversize_write', 'timeout_write']:
                if name == 'close' and mode == 'duplicate_write': continue
                self.state = {'mode': mode, 'rows': [] if name in ['link_task','comment','close'] else [{'body': LINK, 'author':'ahu-bot'}], 'issue':{'number':7,'state':'OPEN','updatedAt':'r1'}}; self.save()
                data = action(name)
                if name in ['comment','close','task_evidence']: data['operation_id'] = 'op'
                if name in ['comment','task_evidence']: data['body'] = 'ok'
                if name == 'task_evidence': data['kind'] = 'review'
                if name in ['link_task','sync_task','task_evidence']: data = observed(data)
                if name == 'sync_replay': data = observed(data, changes=[dict(revision=4,kind='submitted',lifecycle='running',cancellation_requested=False)])
                with self.subTest(action=name, mode=mode):
                    self.effect(data, 'outcome_unknown', timeout=800)
                    self.assertLessEqual(self.load().get('writes', 0), 1)

    def test_provider_failures_and_diagnostics_are_bounded(self):
        for mode in ['failure','duplicate_json','oversize','null_repository','missing_actor']:
            self.state['mode'] = mode; self.save()
            self.assertEqual(self.invoke(action('comment', body='ok', operation_id='op'))['status'], 'error')
        self.assertFalse(self.load().get('writes'))

    def test_timeout_eof_and_leader_exit_kill_descendants(self):
        for mode in ['timeout','leader_exit','closed_stdout']:
            self.state['mode'] = mode; self.save()
            start = time.monotonic()
            self.assertEqual(self.invoke(dict(action='issue',repository='org/repo',number=7), timeout=150)['status'], 'error')
            self.assertLess(time.monotonic()-start, 2)
            path = self.home/'descendant'
            if path.exists():
                pid = int(path.read_text())
                # A dead child may remain a zombie until the OS reaper runs.
                status = subprocess.run(['/bin/ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
                self.assertTrue(not status or status.startswith('Z'), status)
                path.unlink()

    def test_all_mutations_refuse_stale_state_before_dispatch(self):
        for name in ['comment', 'close', 'link_task', 'sync_task', 'task_evidence', 'sync_replay']:
            self.state = {'rows': [] if name in ['comment', 'close', 'link_task'] else [{'body': LINK, 'author': 'ahu-bot'}],
                          'issue': {'number': 7, 'state': 'OPEN', 'updatedAt': 'newer'}}
            self.save()
            data = action(name)
            if name in ['comment', 'close', 'task_evidence']: data['operation_id'] = 'op'
            if name in ['comment', 'task_evidence']: data['body'] = 'ok'
            if name == 'task_evidence': data['kind'] = 'review'
            if name in ['link_task', 'sync_task', 'task_evidence']: data = observed(data)
            if name == 'sync_replay': data = observed(data, changes=[dict(revision=4, kind='submitted', lifecycle='running', cancellation_requested=False)])
            with self.subTest(action=name):
                self.assertEqual(self.invoke(data)['status'], 'error')
                self.assertFalse(self.load().get('writes'))
        self.assertFalse(any(args[:2] in [['issue', 'comment'], ['issue', 'close']] for args in self.calls()))

    def test_unavailable_provider_is_not_reported_as_a_dispatched_mutation(self):
        self.gh.unlink()
        response = self.invoke(action('comment', body='ok', operation_id='op'))
        self.assertEqual(response, {'protocol_version': 1, 'status': 'error', 'code': 'unsupported'})
        self.assertEqual(self.calls(), [])

    def test_production_binaries_reject_frames_without_provider_activation(self):
        for binary in ['ahu-github-cli-plugin', 'ahu-github-tracker-plugin']:
            process = subprocess.run([str(TARGET/binary), '--ahu-plugin-protocol=1'], input=b'{}', capture_output=True, timeout=3)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(process.stdout)['status'], 'error')
            self.assertEqual(process.stderr, b'')

    def test_stalled_stdin_is_bounded(self):
        process = subprocess.Popen([str(TARGET/'ahu-github-tracker-plugin'), '--ahu-plugin-protocol=1'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            process.wait(timeout=4)
            self.assertEqual(json.loads(process.stdout.read())['status'], 'error')
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdin.close(); process.stdout.close(); process.stderr.close()

if __name__ == '__main__': unittest.main()
