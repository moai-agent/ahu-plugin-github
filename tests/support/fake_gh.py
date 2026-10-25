# Development fixture only. No network access or real authentication.
import json
import os
import pathlib
import subprocess
import sys
import time

home = pathlib.Path(os.environ['HOME'])
path = home / 'state.json'
state = json.loads(path.read_text())
args = sys.argv[1:]
with (home / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
mode = state.get('mode')
mutation = args[:2] in [['issue', 'comment'], ['issue', 'close']]
if mode == 'failure':
    print('PRIVATE DIAGNOSTIC TOKEN', file=sys.stderr)
    sys.exit(1)
if mode == 'missing_actor' and args == ['api', 'user']:
    print('{}')
    sys.exit()
if mode == 'duplicate_json':
    print('{"number":7,"number":7}')
    sys.exit()
if mode == 'oversize' or mode == 'oversize_write' and mutation:
    sys.stdout.write('x' * 65537)
    sys.exit()
if mode == 'timeout' or mode == 'timeout_write' and mutation:
    child = subprocess.Popen(['/bin/sleep', '30'])
    (home / 'descendant').write_text(str(child.pid))
    time.sleep(30)
if mode == 'leader_exit':
    child = subprocess.Popen(['/bin/sleep', '30'])
    (home / 'descendant').write_text(str(child.pid))
    sys.exit()
if mode == 'closed_stdout':
    os.close(1)
    time.sleep(30)
if mutation:
    if args[:2] == ['issue', 'comment']:
        assert args[3:6] == ['--repo', 'org/repo', '--body'], args
        state['rows'].append({'body': args[-1], 'author': 'Ahu-Bot'})
        if mode == 'duplicate_write':
            state['rows'].append(dict(state['rows'][-1]))
    else:
        assert args[3:] == ['--repo', 'org/repo', '--reason', 'completed'], args
        state['issue']['state'] = 'CLOSED'
    state['writes'] = state.get('writes', 0) + 1
    state['issue']['updatedAt'] = 'r' + str(state['writes'] + 1)
    path.write_text(json.dumps(state))
    if mode == 'fail_write':
        sys.exit(1)
    print('https://example.invalid/comment/1')
    sys.exit()
if state.get('writes') and mode == 'fail_verify':
    print('PRIVATE DIAGNOSTIC TOKEN', file=sys.stderr)
    sys.exit(1)
if args[:2] == ['repo', 'view']:
    assert args[2:] in [['org/repo', '--json', 'nameWithOwner,visibility'], ['org/repo', '--json', 'nameWithOwner,url,defaultBranchRef']], args
    value = {'nameWithOwner': 'org/repo', 'visibility': state.get('visibility', 'PUBLIC'), 'url': 'https://example.invalid/repo', 'defaultBranchRef': {'name': 'main'}, 'secret': 'not projected'}
    if mode == 'null_repository':
        value['nameWithOwner'] = None
elif args[:2] == ['issue', 'view']:
    assert args[2:] == ['7', '--repo', 'org/repo', '--json', 'number,title,state,url,updatedAt'], args
    value = state['issue']
elif args[:2] == ['issue', 'list']:
    assert args[2:6] == ['--repo', 'org/repo', '--state', 'open'], args
    assert args[-4:] == ['--limit', '50', '--json', 'number,title,state,url,updatedAt'], args
    value = [state['issue']]
elif args[:2] == ['release', 'list']:
    assert args[2:] == ['--repo', 'org/repo', '--limit', '20', '--json', 'tagName,name,isLatest,publishedAt'], args
    value = [{'tagName': 'v1', 'name': 'one', 'isLatest': True, 'publishedAt': 'now'}]
elif args == ['api', 'user']:
    value = {'login': 'Ahu-Bot'}
else:
    assert args[:3] == ['api', '--paginate', '--jq'], args
    assert args[-1] == 'repos/org/repo/issues/7/comments', args
    assert '{body: .body, author: .user.login}' in args[3]
    assert '--slurp' not in args
    # Return consecutive independently encoded pages, like native gh --jq.
    print(json.dumps(state['rows'][:1]))
    value = state['rows'][1:]
print(json.dumps(value))
