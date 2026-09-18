"""CI-only: two disposable nodes, real iperf3, pinned TLS, team start and pause."""
import hashlib
import http.client
import json
import secrets
import ssl
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()


def api(node, path, payload=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection('127.0.0.1', node['port'], context=context, timeout=30)
    try:
        connection.connect()
        assert hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() == node['fingerprint']
        headers = {'Content-Type': 'application/json'}
        if node.get('cookie'):
            headers.update(Cookie=node['cookie'], **{'X-CSRF-Token': node['csrf']})
        connection.request('POST' if payload is not None else 'GET', path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        if path == '/api/login' and response.status == 200:
            node.update(cookie=response.getheader('Set-Cookie').split(';')[0], csrf=result['csrf'])
        assert response.status < 400, f'{path}: {result.get("error", "HTTP error")}'
        return result
    finally:
        connection.close()


def wait_for(check, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, AssertionError, ValueError):
            pass
        time.sleep(.5)
    raise AssertionError('Smoke test timeout')


def main():
    suffix = secrets.token_hex(4)
    network = 'cf-smoke-' + suffix
    names = ['cf-a-' + suffix, 'cf-b-' + suffix]
    nodes = []
    docker('network', 'create', network)
    try:
        for name in names:
            docker('run', '-d', '--name', name, '--network', network, '--init',
                   '--read-only', '--tmpfs', '/tmp', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges:true',
                   '-p', '127.0.0.1::8443', '-e', 'NODE_NAME=' + name, 'clusterfuck:test')
            def credentials():
                code = "import json,ssl,hashlib; from pathlib import Path; d=json.loads(Path('/data/credentials.json').read_text()); d['fingerprint']=hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path('/data/tls.crt').read_text())).hexdigest(); print(json.dumps(d))"
                try:
                    return json.loads(docker('exec', name, 'python', '-c', code))
                except subprocess.CalledProcessError:
                    return None
            credential = wait_for(credentials)
            port = int(docker('port', name, '8443/tcp').split(':')[-1])
            node = dict(name=name, port=port, fingerprint=credential['fingerprint'], credential=credential)
            wait_for(lambda: api(node, '/api/login', {'password': credential['password']}))
            wait_for(lambda: api(node, '/api/node')['server_running'])
            nodes.append(node)
        for index, node in enumerate(nodes):
            other = nodes[1 - index]
            cfg = api(node, '/api/config')
            cfg.update(public_url='https://' + node['name'] + ':8443', public_host=node['name'], public_iperf_port=5201)
            cfg['peers'] = [dict(id='remote', name=other['name'], url='https://' + other['name'] + ':8443',
                                token=other['credential']['peer_token'], fingerprint=other['fingerprint'])]
            cfg['targets'] = [dict(id='a', name='Node A', host=nodes[0]['name'], port=5201, node_id='self' if index == 0 else 'remote'),
                              dict(id='b', name='Node B', host=nodes[1]['name'], port=5201, node_id='remote' if index == 0 else 'self')]
            api(node, '/api/config', cfg)
        first = nodes[0]
        assert api(first, '/api/dashboard')['peers'][0]['reachable']
        probe = api(first, '/api/external-check', {'peer_id': 'remote'})
        assert probe['api'] and probe['iperf'] == 'bereit', probe
        run = api(first, '/api/team', {'tasks': [{'node_id': 'self', 'target_id': 'b'}, {'node_id': 'remote', 'target_id': 'a'}],
                                      'options': {'duration': 4}})
        seen_live = set()
        deadline = time.time() + 40
        while time.time() < deadline:
            snapshots = [api(n, '/api/node') for n in nodes]
            jobs = [next(j for j in s['jobs'] if j['group_id'] == run['group_id']) for s in snapshots]
            for index, job in enumerate(jobs):
                if job['status'] == 'running' and job['intervals']:
                    seen_live.add(index)
            if all(j['status'] == 'completed' for j in jobs):
                break
            assert not any(j['status'] in ('failed', 'cancelled') for j in jobs), jobs
            time.sleep(.4)
        assert all(j['status'] == 'completed' for j in jobs), jobs
        assert seen_live == {0, 1}, 'Both nodes must report live intervals'
        assert abs(jobs[0]['actual_start'] - jobs[1]['actual_start']) < 1
        cfg = api(first, '/api/config')
        cfg['paused_until'] = -1
        api(first, '/api/config', cfg)
        wait_for(lambda: not api(first, '/api/node')['server_running'])
        result = subprocess.run(['docker', 'exec', nodes[1]['name'], 'iperf3', '-c', first['name'], '--connect-timeout', '1000', '-t', '1'], capture_output=True)
        assert result.returncode != 0, 'Pause must also block direct incoming iperf3'
        print('PASS: real iperf3, pinned HTTPS, bidirectional peer control, live team data, external probe and incoming pause')
    except Exception:
        # Application logs never contain secrets. Print diagnostics before cleanup.
        for name in names:
            subprocess.run(['docker', 'logs', '--tail', '30', name])
            subprocess.run(['docker', 'exec', name, 'iperf3', '--version'])
        raise
    finally:
        for name in names:
            subprocess.run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
