"""Identical peer nodes; no permanent coordinator or external service."""
import concurrent.futures
import copy
import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .core import blocked, command, ensure_window, initial_config, integer, load, options, save, validate_config

ACTIVE = ('prepared', 'waiting', 'running')


def peer_request(peer, path, payload=None, insecure=False):
    address = urlsplit(peer['url'])
    if address.scheme == 'https':
        # Verify the pinned certificate before transmitting credentials.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(address.hostname, address.port or 443, timeout=5, context=context)
        connection.connect()
        actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, peer.get('fingerprint', '')):
            connection.close()
            raise ValueError('Zertifikatfingerabdruck stimmt nicht überein')
    elif address.scheme == 'http' and insecure:
        connection = http.client.HTTPConnection(address.hostname, address.port or 80, timeout=5)
    else:
        raise ValueError('Unsichere Peer-Verbindung abgelehnt')
    try:
        headers = {'Authorization': 'Bearer ' + peer['token'], 'Content-Type': 'application/json'}
        connection.request('POST' if payload is not None else 'GET', path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('Peer-Antwort zu groß')
        result = json.loads(raw)
        if response.status >= 400:
            raise ValueError(result.get('error', 'Peer-Auftrag abgelehnt'))
        return result
    finally:
        connection.close()


class Node:
    def __init__(self, data_dir, start_workers=True):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix':
            os.chmod(self.directory, 0o700)
        self.lock = threading.RLock()
        self.insecure = os.getenv('ALLOW_INSECURE_PEERS', 'false').lower() == 'true'
        self.config = load(self.directory / 'config.json', initial_config())
        self.credentials = load(self.directory / 'credentials.json', dict(
            password=secrets.token_urlsafe(24), peer_token=secrets.token_urlsafe(32), node_id=secrets.token_hex(12)))
        save(self.directory / 'credentials.json', self.credentials)
        save(self.directory / 'config.json', self.config)
        self.jobs = load(self.directory / 'jobs.json', [])
        for job in self.jobs:
            if job['status'] in ACTIVE:
                job.update(status='interrupted', error='Node wurde neu gestartet')
        self.processes = {}
        self.sessions = {}
        self.login_attempts = {}
        self.server_process = None
        self.server_error = ''
        self.server_busy = False
        self.server_started_at = 0
        self.stop_event = threading.Event()
        self.fingerprint = ''
        self.iperf_port = int(os.getenv('IPERF_PORT', '5201'))
        self.start_workers = start_workers
        self.checks = {}
        self.monitor = None
        if start_workers:
            self.monitor = threading.Thread(target=self.maintenance, daemon=True)
            self.monitor.start()

    def persist_jobs(self):
        with self.lock:
            active = [j for j in self.jobs if j['status'] in ACTIVE]
            finished = [j for j in self.jobs if j['status'] not in ACTIVE][-100:]
            for job in finished:
                points = job['intervals']
                if len(points) > 120:
                    job['intervals'] = [points[round(i * (len(points) - 1) / 119)] for i in range(120)]
            self.jobs = finished + active
            save(self.directory / 'jobs.json', self.jobs)

    def public_config(self):
        with self.lock:
            result = copy.deepcopy(self.config)
            for peer in result['peers']:
                peer['token'] = ''
            return result

    def update_config(self, cfg):
        with self.lock:
            self.config = validate_config(cfg, self.config, self.insecure)
            save(self.directory / 'config.json', self.config)
        return self.public_config()

    def readiness(self):
        with self.lock:
            reason = blocked(self.config)
            if not reason and any(j['status'] in ACTIVE for j in self.jobs):
                reason = 'Test läuft oder ist reserviert'
            return dict(ready=not bool(reason), reason=reason)

    def snapshot(self):
        with self.lock:
            result = dict(id=self.credentials['node_id'], name=self.config['name'], timestamp=time.time(),
                          **self.readiness(), blocked=blocked(self.config),
                          server_enabled=self.config['server_enabled'],
                          server_running=bool(self.server_process and self.server_process.poll() is None),
                          server_busy=self.server_busy,
                          server_error=self.server_error, iperf_port=self.iperf_port,
                          jobs=copy.deepcopy(self.jobs[-30:]), version='1.0.0')
            # Bound polling traffic even for hour-long tests; retain the latest point.
            for job in result['jobs']:
                points = job['intervals']
                if len(points) > 120:
                    job['intervals'] = [points[round(i * (len(points) - 1) / 119)] for i in range(120)]
        return result

    def peer(self, peer_id):
        with self.lock:
            for peer in self.config['peers']:
                if peer['id'] == peer_id:
                    return copy.deepcopy(peer)
        raise ValueError('Unbekannter Node')

    def request(self, peer_id, path, data=None):
        return peer_request(self.peer(peer_id), path, data, self.insecure)

    def dashboard(self):
        result = {'self': self.snapshot(), 'peers': [], 'checks': copy.deepcopy(self.checks)}
        with self.lock:
            peers = copy.deepcopy(self.config['peers'])
        def fetch(peer):
            try:
                data = peer_request(peer, '/api/node', insecure=self.insecure)
                return dict(key=peer['id'], reachable=True, data=data,
                            clock_offset=round(data['timestamp'] - time.time(), 2))
            except (OSError, ValueError, http.client.HTTPException):
                return dict(key=peer['id'], reachable=False, name=peer.get('name', peer['id']), error='Verbindung oder Authentifizierung fehlgeschlagen')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            result['peers'] = list(executor.map(fetch, peers))
        return result

    def receive_check(self, data):
        with self.lock:
            if not self.config['server_enabled'] or not self.server_process or self.server_process.poll() is not None:
                raise ValueError('Empfangender iperf3-Server ist nicht bereit')
            if self.server_busy:
                raise ValueError('Empfangender iperf3-Server ist belegt')
            start = float(data['start_at'])
            if not time.time() - 5 <= start <= time.time() + 60:
                raise ValueError('Ungültige Empfangs-Startzeit')
            ensure_window(self.config, start, integer(data['seconds'], 1, 3640, 'Empfangsdauer'))
        return {'ready': True}

    def prepare(self, data):
        settings = options(data['options'])
        target = dict(host=data['target']['host'], port=data['target']['port'], name=data['target'].get('name', 'Testziel'))
        command(target, settings)
        start = float(data['start_at'])
        if not time.time() + 1 <= start <= time.time() + 60:
            raise ValueError('Startzeit muss 1–60 Sekunden in der Zukunft liegen')
        with self.lock:
            if not self.readiness()['ready']:
                raise ValueError(self.readiness()['reason'])
            ensure_window(self.config, start, settings['duration'] + settings['omit'] + 10)
            job = dict(id=secrets.token_hex(12), group_id=str(data.get('group_id', ''))[:80],
                       target=target, options=settings, status='prepared', created_at=time.time(),
                       start_at=start, actual_start=None, finished_at=None, expires_at=time.time() + 30,
                       intervals=[], summary=None, error='')
            self.jobs.append(job)
            self.persist_jobs()
            return copy.deepcopy(job)

    def find_job(self, job_id):
        for job in self.jobs:
            if job['id'] == job_id:
                return job
        raise ValueError('Unbekannter Test')

    def commit(self, job_id):
        with self.lock:
            job = self.find_job(job_id)
            if job['status'] != 'prepared' or job['expires_at'] <= time.time() or job['start_at'] <= time.time():
                raise ValueError('Reservierung abgelaufen oder Startzeit überschritten')
            ensure_window(self.config, job['start_at'], job['options']['duration'] + job['options']['omit'] + 10)
            job['status'] = 'waiting'
            self.persist_jobs()
            if self.start_workers:
                threading.Thread(target=self.run_job, args=(job_id,), daemon=True).start()
            return copy.deepcopy(job)

    def cancel(self, job_id, reason='Abgebrochen'):
        with self.lock:
            job = self.find_job(job_id)
            if job['status'] in ACTIVE:
                job.update(status='cancelled', error=reason, finished_at=time.time())
                process = self.processes.get(job_id)
                if process and process.poll() is None:
                    process.terminate()
                self.persist_jobs()
            return copy.deepcopy(job)

    def run_job(self, job_id):
        with self.lock:
            job = self.find_job(job_id)
            delay = max(0, job['start_at'] - time.time())
        if self.stop_event.wait(delay):
            return
        process = None
        try:
            with self.lock:
                if job['status'] != 'waiting':
                    return
                ensure_window(self.config, time.time(), job['options']['duration'] + job['options']['omit'] + 10)
                process = subprocess.Popen(command(job['target'], job['options']), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
                self.processes[job_id] = process
                job.update(status='running', actual_start=time.time())
            watchdog = threading.Timer(job['options']['duration'] + job['options']['omit'] + 20,
                                       lambda: self.cancel(job_id, 'Zeitlimit überschritten'))
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.record_event(job, event)
                returncode = process.wait(timeout=5)
            finally:
                watchdog.cancel()
            with self.lock:
                if job['status'] == 'running':
                    if job['error'] or returncode != 0 or job['summary'] is None:
                        job.update(status='failed', error=job['error'] or f'iperf3 beendet ohne Ergebnis (Code {returncode})')
                    else:
                        job['status'] = 'completed'
                    job['finished_at'] = time.time()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            with self.lock:
                if job['status'] not in ('cancelled', 'completed'):
                    job.update(status='failed', error=str(error)[:300], finished_at=time.time())
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            with self.lock:
                self.processes.pop(job_id, None)
                self.persist_jobs()

    def record_event(self, job, event):
        with self.lock:
            if not isinstance(event, dict):
                return
            kind = event.get('event')
            data = event.get('data', {})
            if kind != 'error' and not isinstance(data, dict):
                return
            if kind == 'interval':
                # Keep sender/receiver and bidirectional sums separate.
                sums = {k: v for k, v in data.items() if k.startswith('sum') and isinstance(v, dict)}
                if not sums:
                    sums = {'sum': {'bits_per_second': sum(s.get('bits_per_second', 0) for s in data.get('streams', []))}}
                job['intervals'].append(dict(at=time.time(), sums=sums))
                job['intervals'] = job['intervals'][-3700:]
            elif kind == 'end':
                job['summary'] = {k: v for k, v in data.items() if k.startswith('sum') or k == 'cpu_utilization_percent'}
            elif kind == 'error':
                job['error'] = str(data)[:300]
            if event.get('error'):
                job['error'] = str(event['error'])[:300]

    def team(self, payload):
        tasks = payload['tasks']
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError('1–32 Teilnehmer erforderlich')
        settings = options(payload['options'])
        with self.lock:
            targets = {t['id']: copy.deepcopy(t) for t in self.config['targets']}
        selected = set()
        endpoints = set()
        resolved = []
        for task in tasks:
            node_id = task['node_id']
            if node_id in selected:
                raise ValueError('Jeder Node darf nur einmal teilnehmen')
            selected.add(node_id)
            target = targets.get(task['target_id'])
            if not target:
                raise ValueError('Unbekanntes Testziel')
            endpoint = (target['host'].lower(), int(target['port']))
            if endpoint in endpoints:
                raise ValueError('Gleichzeitige unabhängige Tests benötigen unterschiedliche Zielports')
            endpoints.add(endpoint)
            if node_id != 'self':
                remote = self.request(node_id, '/api/node')
                if abs(remote['timestamp'] - time.time()) > 1:
                    raise ValueError('Systemuhren weichen um mehr als 1 Sekunde ab; NTP prüfen')
            resolved.append((node_id, target))
        # Reserve concurrently so slow/unreachable peers do not shift start times.
        start_at = time.time() + 15
        group_id = secrets.token_hex(10) if len(resolved) > 1 else ''
        receipts = []
        def reserve(item):
            node_id, target = item
            receiver = target.get('node_id')
            if receiver:
                check = dict(start_at=start_at, seconds=settings['duration'] + settings['omit'] + 10)
                if receiver == 'self':
                    self.receive_check(check)
                else:
                    self.request(receiver, '/api/receive', check)
            data = dict(target=target, options=settings, start_at=start_at, group_id=group_id)
            return node_id, self.prepare(data) if node_id == 'self' else self.request(node_id, '/api/prepare', data)
        try:
            errors = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(reserve, item) for item in resolved]
                for future in futures:
                    try:
                        receipts.append(future.result())
                    except Exception as error:
                        errors.append(str(error))
            if errors:
                raise ValueError('; '.join(errors))
            if time.time() >= start_at - 5:
                raise ValueError('Vorbereitung dauerte zu lange; kein Gruppenstart')
            def confirm(receipt):
                node_id, job = receipt
                return self.commit(job['id']) if node_id == 'self' else self.request(node_id, '/api/commit', {'id': job['id']})
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
                futures = [executor.submit(confirm, receipt) for receipt in receipts]
                for future in futures:
                    future.result()
        except Exception:
            for node_id, job in receipts:
                try:
                    if node_id == 'self':
                        self.cancel(job['id'], 'Gruppenstart fehlgeschlagen')
                    else:
                        self.request(node_id, '/api/cancel', {'id': job['id']})
                except Exception:
                    pass  # Prepared reservations expire; committed failures are documented.
            raise
        return dict(group_id=group_id, start_at=start_at, jobs=[dict(node_id=n, id=j['id']) for n, j in receipts])

    def probe(self, payload):
        # Probe only a previously paired peer, never an arbitrary unauthenticated URL.
        peer_id = payload['peer_id']
        peer = self.peer(peer_id)
        node = peer_request(peer, '/api/node', insecure=self.insecure)
        host = payload['host']
        port = int(payload['port'])
        if host != urlsplit(peer['url']).hostname:
            raise ValueError('Probe-Host muss dem konfigurierten Peer-Host entsprechen')
        try:
            with socket.create_connection((host, port), timeout=3) as connection:
                cookie = secrets.token_hex(18).encode()[:36] + b'\x00'
                connection.sendall(cookie)
                state = connection.recv(1)
            iperf = 'bereit' if state == b'\x09' else 'belegt' if state == b'\xff' else 'unerwartete Antwort'
        except OSError:
            iperf = 'nicht erreichbar'
        return dict(api=True, iperf=iperf, name=node['name'], checked_at=time.time(), perspective=self.config['name'])

    def external_check(self, payload):
        peer_id = payload['peer_id']
        with self.lock:
            public_url = self.config['public_url'].rstrip('/')
            public_host = self.config['public_host']
            port = self.config['public_iperf_port']
        if not public_url or not public_host:
            raise ValueError('Eigene öffentliche API-Adresse und iperf-Host zuerst konfigurieren')
        remote = self.request(peer_id, '/api/node')
        # The remote must already have this node paired under its own local peer ID.
        result = self.request(peer_id, '/api/probe-self', {
            'node_id': self.credentials['node_id'], 'url': public_url, 'host': public_host, 'port': port})
        result['remote_node_id'] = remote['id']
        with self.lock:
            self.checks[peer_id] = result
        return result

    def probe_self(self, payload):
        with self.lock:
            peers = copy.deepcopy(self.config['peers'])
        for peer in peers:
            if peer['url'].rstrip('/') == payload['url'].rstrip('/'):
                remote = peer_request(peer, '/api/node', insecure=self.insecure)
                if remote['id'] != payload['node_id']:
                    raise ValueError('Node-Identität passt nicht zur öffentlichen Adresse')
                return self.probe(dict(peer_id=peer['id'], host=payload['host'], port=payload['port']))
        raise ValueError('Der prüfende Node muss deine öffentliche Adresse als Peer eingetragen haben')

    def maintenance(self):
        next_retry = 0
        while not self.stop_event.wait(0.5):
            with self.lock:
                reason = blocked(self.config)
                for job in list(self.jobs):
                    if job['status'] == 'prepared' and job['expires_at'] < time.time():
                        self.cancel(job['id'], 'Reservierung abgelaufen')
                    elif reason and job['status'] in ACTIVE:
                        self.cancel(job['id'], reason)
                wanted = self.config['server_enabled'] and not reason
                running = self.server_process and self.server_process.poll() is None
                if running and self.server_busy and time.time() - self.server_started_at > 3630:
                    self.server_process.terminate()
                    self.server_error = 'Eingehender Test hat das Zeitlimit überschritten'
                if running and not wanted:
                    self.server_process.terminate()
                elif wanted and not running and time.time() >= next_retry:
                    try:
                        self.server_process = subprocess.Popen([
                            os.getenv('IPERF_BINARY', 'iperf3'), '-s', '-p', str(self.iperf_port),
                            '--json-stream', '--forceflush'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
                        self.server_busy = False
                        threading.Thread(target=self.read_server, args=(self.server_process,), daemon=True).start()
                        self.server_error = ''
                    except OSError:
                        self.server_error = 'iperf3-Server konnte nicht gestartet werden'
                    next_retry = time.time() + 5
                elif wanted and self.server_process and self.server_process.poll() is not None:
                    self.server_error = 'iperf3-Server beendet; Port/Installation prüfen'

    def read_server(self, process):
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                with self.lock:
                    if self.server_process is process:
                        if event.get('event') == 'start':
                            self.server_busy = True
                            self.server_started_at = time.time()
                        elif event.get('event') in ('end', 'error'):
                            self.server_busy = False
        finally:
            process.stdout.close()
            process.wait()
            with self.lock:
                if self.server_process is process:
                    self.server_busy = False

    def close(self):
        self.stop_event.set()
        if self.monitor:
            self.monitor.join(timeout=2)
        with self.lock:
            for job in list(self.jobs):
                if job['status'] in ACTIVE:
                    self.cancel(job['id'], 'Node beendet')
            if self.server_process and self.server_process.poll() is None:
                self.server_process.terminate()
                try:
                    self.server_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.server_process.kill()
                    self.server_process.wait()
