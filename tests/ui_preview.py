"""Local browser QA only. No iperf traffic and no public deployment."""
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['ALLOW_INSECURE_PEERS'] = 'true'
from clusterfuck.runtime import Node
from clusterfuck.server import make_handler

node = Node(Path(__file__).resolve().parent.parent / 'test-data' / 'ui-preview', start_workers=False)
node.credentials['password'] = 'local-ui-test-only'
server = ThreadingHTTPServer(('127.0.0.1', 18765), make_handler(node))
print('Local UI QA: http://127.0.0.1:18765', flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    node.close()
