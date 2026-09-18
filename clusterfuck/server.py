import hashlib
import hmac
import json
import os
import secrets
import signal
import ssl
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import Node

STATIC = Path(__file__).parent / 'static'
PEER_PATHS = {'/api/node', '/api/prepare', '/api/commit', '/api/cancel', '/api/receive', '/api/probe-self'}


def make_handler(node):
    # Browsers scope cookies to hosts, not ports: two local nodes need distinct names.
    cookie_name = 'cf_session_' + node.credentials['node_id'][:12]
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ClusterFuck/1.0'

        def setup(self):
            self.request.settimeout(15)
            super().setup()

        def log_message(self, *args):
            pass  # Do not log credentials, request bodies or pairing tokens.

        def respond(self, data, status=200, content_type='application/json', cookie=None):
            raw = json.dumps(data).encode() if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                return None
            key = cookie.get(cookie_name)
            with node.lock:
                result = node.sessions.get(key.value if key else '')
                return result if result and result['expires'] > time.time() else None

        def authorized(self, path, mutation=False):
            bearer = self.headers.get('Authorization', '')
            if bearer.startswith('Bearer '):
                return path in PEER_PATHS and hmac.compare_digest(bearer[7:].encode(), node.credentials['peer_token'].encode())
            session = self.session()
            if not session:
                return False
            if mutation:
                return hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), session['csrf'].encode())
            return True

        def body(self):
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Content-Type application/json erforderlich')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 262144:
                raise ValueError('Ungültige Request-Größe')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('JSON-Objekt erforderlich')
            return data

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                return self.respond({'status': 'ok'})
            if path in ('/', '/app.js', '/style.css'):
                file = STATIC / {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                kind = {'/': 'text/html', '/app.js': 'application/javascript', '/style.css': 'text/css'}[path]
                return self.respond(file.read_bytes(), content_type=kind)
            if not self.authorized(path):
                return self.respond({'error': 'Anmeldung erforderlich'}, 401)
            try:
                if path == '/api/session':
                    return self.respond({'csrf': self.session()['csrf']})
                if path == '/api/node':
                    return self.respond(node.snapshot())
                if path == '/api/dashboard':
                    return self.respond(node.dashboard())
                if path == '/api/config':
                    return self.respond(node.public_config())
                if path == '/api/pairing':
                    return self.respond(dict(token=node.credentials['peer_token'], fingerprint=node.fingerprint,
                                             node_id=node.credentials['node_id']))
                return self.respond({'error': 'Nicht gefunden'}, 404)
            except (OSError, ValueError) as error:
                return self.respond({'error': str(error)[:300]}, 400)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                origin = self.headers.get('Origin')
                if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                    return self.respond({'error': 'Fremder Origin abgelehnt'}, 403)
                data = self.body()
                if path == '/api/login':
                    address = self.client_address[0]
                    with node.lock:
                        node.login_attempts = {k: v for k, v in node.login_attempts.items() if v['until'] > time.time()}
                        attempt = node.login_attempts.setdefault(address, {'count': 0, 'until': time.time() + 300})
                        if attempt['count'] >= 10:
                            return self.respond({'error': 'Zu viele Versuche; bitte 5 Minuten warten'}, 429)
                        if not hmac.compare_digest(str(data.get('password', '')).encode(), node.credentials['password'].encode()):
                            attempt['count'] += 1
                            return self.respond({'error': 'Passwort falsch'}, 401)
                        node.login_attempts.pop(address, None)
                        node.sessions = {k: v for k, v in node.sessions.items() if v['expires'] > time.time()}
                        if len(node.sessions) > 100:
                            node.sessions.clear()
                        key = secrets.token_urlsafe(32)
                        csrf = secrets.token_urlsafe(24)
                        node.sessions[key] = dict(expires=time.time() + 43200, csrf=csrf)
                    secure = '; Secure' if isinstance(self.connection, ssl.SSLSocket) else ''
                    return self.respond({'csrf': csrf}, cookie=f'{cookie_name}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{secure}')
                if not self.authorized(path, mutation=True):
                    return self.respond({'error': 'Anmeldung/CSRF oder Peer-Authentifizierung ungültig'}, 401)
                if path == '/api/logout':
                    session = self.session()
                    with node.lock:
                        node.sessions = {k: v for k, v in node.sessions.items() if v is not session}
                    return self.respond({'ok': True}, cookie=f'{cookie_name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if path == '/api/config':
                    return self.respond(node.update_config(data))
                if path == '/api/team':
                    return self.respond(node.team(data))
                if path == '/api/prepare':
                    return self.respond(node.prepare(data))
                if path == '/api/commit':
                    return self.respond(node.commit(data['id']))
                if path == '/api/cancel':
                    if data.get('node_id', 'self') == 'self':
                        return self.respond(node.cancel(data['id']))
                    if self.headers.get('Authorization', '').startswith('Bearer '):
                        raise ValueError('Weiterleitung ist nur über das angemeldete WebUI erlaubt')
                    return self.respond(node.request(data['node_id'], '/api/cancel', {'id': data['id']}))
                if path == '/api/receive':
                    return self.respond(node.receive_check(data))
                if path == '/api/probe-self':
                    return self.respond(node.probe_self(data))
                if path == '/api/external-check':
                    return self.respond(node.external_check(data))
                return self.respond({'error': 'Nicht gefunden'}, 404)
            except (KeyError, TypeError, ValueError, OSError) as error:
                return self.respond({'error': str(error)[:300]}, 400)
            except Exception:
                return self.respond({'error': 'Auftrag fehlgeschlagen; Verbindung und Konfiguration prüfen'}, 500)

    return Handler


def serve():
    node = Node(os.getenv('DATA_DIR', '/data'))
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('WEB_PORT', '8443'))), make_handler(node))
    tls = os.getenv('TLS_ENABLED', 'true').lower() == 'true'
    if not tls and not node.insecure:
        raise RuntimeError('HTTP erfordert ALLOW_INSECURE_PEERS=true; nur für lokale Tests')
    if tls:
        cert = node.directory / 'tls.crt'
        key = node.directory / 'tls.key'
        if not cert.exists() or not key.exists():
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '3650',
                            '-subj', '/CN=ClusterFuck'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(key, 0o600)
        node.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('ClusterFuck bereit. Zugangsdaten liegen ausschließlich in /data/credentials.json.', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        node.close()


if __name__ == '__main__':
    serve()
