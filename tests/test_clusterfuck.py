import copy
import datetime as dt
import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import shutil
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from clusterfuck.core import blocked, command, ensure_window, initial_config, validate_config
from clusterfuck.runtime import Node, peer_request
from clusterfuck.server import make_handler

TEST_TEMP = Path(__file__).resolve().parent.parent / 'test-data'
TEST_TEMP.mkdir(exist_ok=True)


class TestDirectory:
    def __init__(self):
        self.path = TEST_TEMP / ('node-' + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        # Only the unique directory created by this fixture, inside the workspace.
        shutil.rmtree(self.path)


def stamp(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = initial_config()
        self.cfg['schedules'] = [dict(days=[4], start='19:00', end='23:00', label='Gaming')]

    def test_friday_boundaries_and_other_days(self):
        self.assertFalse(blocked(self.cfg, stamp('2026-09-18T18:59:59')))
        self.assertEqual(blocked(self.cfg, stamp('2026-09-18T19:00:00')), 'Gaming')
        self.assertTrue(blocked(self.cfg, stamp('2026-09-18T22:59:59')))
        self.assertFalse(blocked(self.cfg, stamp('2026-09-18T23:00:00')))
        self.assertFalse(blocked(self.cfg, stamp('2026-09-19T20:00:00')))

    def test_crossing_into_block_rejected(self):
        with self.assertRaises(ValueError):
            ensure_window(self.cfg, stamp('2026-09-18T18:59:40'), 30)

    def test_overnight_and_week_wrap(self):
        self.cfg['schedules'] = [dict(days=[6], start='23:00', end='02:00')]
        self.assertTrue(blocked(self.cfg, stamp('2026-09-20T23:30:00')))
        self.assertTrue(blocked(self.cfg, stamp('2026-09-21T01:30:00')))
        self.assertFalse(blocked(self.cfg, stamp('2026-09-21T02:00:00')))

    def test_dst_uses_node_timezone(self):
        self.cfg['schedules'] = [dict(days=[6], start='02:00', end='03:00')]
        when = dt.datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo('Europe/Berlin'))
        self.assertTrue(blocked(self.cfg, when.replace(fold=0).timestamp()))
        self.assertTrue(blocked(self.cfg, when.replace(fold=1).timestamp()))

    def test_manual_pause(self):
        self.cfg['paused_until'] = -1
        self.assertTrue(blocked(self.cfg))
        self.cfg['paused_until'] = time.time() - 1
        self.cfg['schedules'] = []
        self.assertFalse(blocked(self.cfg))


class ValidationTests(unittest.TestCase):
    def test_arguments_are_validated_and_direction_is_explicit(self):
        args = command({'host': 'speed.example.org', 'port': 5300}, {'duration': 60, 'protocol': 'udp', 'direction': 'bidir', 'bitrate': '50M', 'ip_version': '6'})
        self.assertIn('--json-stream', args)
        self.assertIn('--bidir', args)
        self.assertIn('-6', args)
        self.assertEqual(args[args.index('-t') + 1], '60')
        for target in ('example.org;echo secret', '-s', 'https://example.org'):
            with self.assertRaises(ValueError):
                command({'host': target, 'port': 5201}, {})
        for settings in ({'duration': 0}, {'streams': 99}, {'bitrate': '1M --logfile /tmp/token'}, {'protocol': 'x'}):
            with self.assertRaises(ValueError):
                command({'host': 'example.org', 'port': 5201}, settings)

    def test_blank_peer_token_preserves_secret_and_https_pin_required(self):
        cfg = initial_config()
        cfg['peers'] = [dict(id='b', url='https://example.org:8443', token='x' * 32, fingerprint='a' * 64)]
        updated = copy.deepcopy(cfg)
        updated['peers'][0]['token'] = ''
        self.assertEqual(validate_config(updated, cfg)['peers'][0]['token'], 'x' * 32)
        updated['peers'][0]['fingerprint'] = ''
        with self.assertRaises(ValueError):
            validate_config(updated, cfg)

    def test_http_is_opt_in(self):
        cfg = initial_config()
        cfg['peers'] = [dict(id='b', url='http://node-b:8080', token='x' * 32)]
        with self.assertRaises(ValueError):
            validate_config(cfg, initial_config())
        self.assertEqual(validate_config(cfg, initial_config(), True)['peers'][0]['url'], 'http://node-b:8080')

    def test_wrong_tls_pin_never_transmits_token(self):
        connection = MagicMock()
        connection.sock.getpeercert.return_value = b'untrusted-certificate'
        peer = dict(url='https://example.org:8443', token='secret-only-in-this-fixture', fingerprint='a' * 64)
        with patch('clusterfuck.runtime.http.client.HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'fingerabdruck'):
                peer_request(peer, '/api/node')
        connection.request.assert_not_called()
        connection.close.assert_called_once()


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = Node(self.tmp.name, start_workers=False)
        self.addCleanup(self.node.close)

    def prepared(self):
        return self.node.prepare(dict(target={'host': 'localhost', 'port': 5201}, options={'duration': 2}, start_at=time.time() + 10))

    def test_prepare_commit_and_cancel_release_slot(self):
        job = self.prepared()
        self.assertFalse(self.node.readiness()['ready'])
        with self.assertRaises(ValueError):
            self.prepared()
        self.assertEqual(self.node.commit(job['id'])['status'], 'waiting')
        self.node.cancel(job['id'])
        self.assertTrue(self.node.readiness()['ready'])

    def test_pause_between_prepare_and_commit(self):
        job = self.prepared()
        self.node.config['paused_until'] = -1
        with self.assertRaises(ValueError):
            self.node.commit(job['id'])

    def test_interrupted_history_and_persistent_credentials(self):
        job = self.prepared()
        other = Node(self.tmp.name, start_workers=False)
        self.addCleanup(other.close)
        self.assertEqual(other.credentials, self.node.credentials)
        self.assertEqual(other.find_job(job['id'])['status'], 'interrupted')

    def test_stream_events_keep_both_directions_and_udp_metrics(self):
        job = self.node.find_job(self.prepared()['id'])
        self.node.record_event(job, {'event': 'interval', 'data': {'sum': {'bits_per_second': 1e8}, 'sum_bidir_reverse': {'bits_per_second': 2e8}}})
        self.assertEqual(len(job['intervals'][0]['sums']), 2)
        self.node.record_event(job, {'event': 'end', 'data': {'sum': {'bits_per_second': 1e8, 'jitter_ms': .5, 'lost_percent': 2}, 'streams': ['ignored']}})
        self.assertEqual(job['summary']['sum']['lost_percent'], 2)
        self.assertNotIn('streams', job['summary'])

    def test_live_snapshot_is_bounded_and_preserves_latest_value(self):
        job = self.node.find_job(self.prepared()['id'])
        job['intervals'] = [{'sums': {'sum': {'bits_per_second': i}}} for i in range(3600)]
        snapshot = self.node.snapshot()['jobs'][0]['intervals']
        self.assertEqual(len(snapshot), 120)
        self.assertEqual(snapshot[-1]['sums']['sum']['bits_per_second'], 3599)
        self.assertEqual(len(job['intervals']), 3600)

    def test_team_failure_cancels_successful_reservation(self):
        self.node.config['targets'] = [dict(id='t1', name='S1', host='localhost', port=5201), dict(id='t2', name='S2', host='localhost', port=5202)]
        def request(peer_id, path, data=None):
            if path == '/api/node':
                return {'timestamp': time.time()}
            if path == '/api/prepare':
                raise ValueError('Remote ist pausiert')
        with patch.object(self.node, 'request', side_effect=request):
            with self.assertRaises(ValueError):
                self.node.team({'tasks': [{'node_id': 'self', 'target_id': 't1'}, {'node_id': 'remote', 'target_id': 't2'}], 'options': {}})
        self.assertTrue(self.node.readiness()['ready'])
        self.assertEqual(self.node.jobs[0]['status'], 'cancelled')

    def test_duplicate_server_port_rejected(self):
        self.node.config['targets'] = [dict(id='t', name='S', host='localhost', port=5201)]
        with self.assertRaises(ValueError):
            self.node.team({'tasks': [{'node_id': 'self', 'target_id': 't'}, {'node_id': 'remote', 'target_id': 't'}], 'options': {}})

    def test_real_process_streaming_lifecycle_with_fixture(self):
        script = "import json,time; print(json.dumps({'event':'interval','data':{'sum':{'bits_per_second':123000000}}}),flush=True); time.sleep(.1); print(json.dumps({'event':'end','data':{'sum_sent':{'bits_per_second':123000000}}}),flush=True)"
        import sys
        job = self.node.find_job(self.prepared()['id'])
        job.update(status='waiting', start_at=time.time())
        with patch('clusterfuck.runtime.command', return_value=[sys.executable, '-c', script]):
            self.node.run_job(job['id'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['intervals'][0]['sums']['sum']['bits_per_second'], 123000000)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.node = Node(self.tmp.name, start_workers=False)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.node))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.node.close()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        all_headers = {'Content-Type': 'application/json', **(headers or {})}
        conn.request('POST' if data is not None else 'GET', path, json.dumps(data) if data is not None else None, all_headers)
        response = conn.getresponse()
        status, cookie, raw = response.status, response.getheader('Set-Cookie'), response.read()
        conn.close()
        return status, cookie, json.loads(raw)

    def test_peer_can_read_status_but_not_credentials_or_configuration(self):
        headers = {'Authorization': 'Bearer ' + self.node.credentials['peer_token']}
        self.assertEqual(self.request('/api/node', headers=headers)[0], 200)
        self.assertEqual(self.request('/api/pairing', headers=headers)[0], 401)
        self.assertEqual(self.request('/api/config', headers=headers)[0], 401)
        self.assertEqual(self.request('/api/config', initial_config(), headers)[0], 401)

    def test_login_csrf_and_secret_redaction(self):
        self.assertEqual(self.request('/api/dashboard')[0], 401)
        status, cookie, data = self.request('/api/login', {'password': self.node.credentials['password']})
        self.assertEqual(status, 200)
        headers = {'Cookie': cookie.split(';')[0]}
        self.assertEqual(self.request('/api/config', initial_config(), headers)[0], 401)
        headers['X-CSRF-Token'] = data['csrf']
        cfg = initial_config()
        cfg['peers'] = [dict(id='b', name='B', url='https://example.org:8443', token='x' * 32, fingerprint='a' * 64)]
        result = self.request('/api/config', cfg, headers)
        self.assertEqual(result[0], 200)
        self.assertEqual(result[2]['peers'][0]['token'], '')
        self.assertEqual(self.request('/api/config', headers=headers)[2]['peers'][0]['token'], '')
        self.assertEqual(self.request('/api/login', {'password': 'bad'}, {'Origin': 'https://evil.example'})[0], 403)

    def test_two_nodes_exchange_status_over_real_http(self):
        peer = dict(url=f'http://127.0.0.1:{self.port}', token=self.node.credentials['peer_token'])
        self.assertEqual(peer_request(peer, '/api/node', insecure=True)['id'], self.node.credentials['node_id'])
        peer['token'] = 'wrong'
        with self.assertRaises(ValueError):
            peer_request(peer, '/api/node', insecure=True)


if __name__ == '__main__':
    unittest.main()
