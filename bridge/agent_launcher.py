"""Opt-in local terminal launcher. Run in the project agents may access."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def command_for(client, action, url, token):
    if client not in ('claude', 'codex') or action not in ('new', 'resume'):
        raise ValueError('Unsupported client or action')
    config = {'mcpServers': {'bridge': {'type': 'http', 'url': url,
              'headers': {'Authorization': 'Bearer ' + token}}}}
    if client == 'claude':
        return ['claude', '--mcp-config', json.dumps(config)] + (['--resume'] if action == 'resume' else [])
    return ['codex', '-c', 'mcp_servers.bridge.url=' + json.dumps(url),
            '-c', 'mcp_servers.bridge.http_headers=' + '{Authorization=' + json.dumps('Bearer ' + token) + '}'] + (
                ['resume'] if action == 'resume' else [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge', default='http://localhost:7333')
    args = parser.parse_args()
    origin = args.bridge.rstrip('/')
    parts = urlsplit(origin)
    if parts.scheme != 'http' or parts.hostname not in ('localhost', '127.0.0.1') or parts.path:
        parser.error('The launcher currently pairs only with a local HTTP Raven origin')
    project = Path.cwd().resolve()
    secret = secrets.token_urlsafe(32)
    launches = threading.Lock()
    temporary = tempfile.TemporaryDirectory(prefix='bridge-launcher-')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log credentials or request payloads.

        def trusted(self):
            return (self.headers.get('Origin') == origin and
                    self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}')

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            if self.trusted():
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Bridge-Launcher')
                self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.reply(200 if self.trusted() else 403, {})

        def do_POST(self):
            if not self.trusted() or not secrets.compare_digest(self.headers.get('X-Bridge-Launcher', ''), secret):
                return self.reply(403, {'error': 'Launcher pairing required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16000:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Expected an object')
                if self.path == '/status':
                    return self.reply(200, {'project': str(project), 'clients': {
                        name: bool(shutil.which(name)) for name in ('claude', 'codex')}})
                if self.path != '/launch':
                    return self.reply(404, {'error': 'Unknown action'})
                token = data.get('token', '')
                if not isinstance(token, str) or not token or len(token) > 4096:
                    raise ValueError('An agent credential is required')
                command = command_for(data.get('client'), data.get('action'), origin + '/mcp', token)
                executable = shutil.which(command[0])
                if not executable:
                    raise ValueError('Install and sign into this agent CLI first')
                command[0] = executable
                if not launches.acquire(blocking=False):
                    raise ValueError('A launch is already being prepared')
                try:
                    script = Path(temporary.name) / (secrets.token_hex(12) + '.command')
                    script.write_text('#!/bin/sh\ncd ' + shlex.quote(str(project)) + '\nexec ' + shlex.join(command) + '\n')
                    script.chmod(0o700)
                    if os.uname().sysname == 'Darwin':
                        subprocess.run(['open', '-a', 'Terminal', str(script)], check=True)
                    elif shutil.which('x-terminal-emulator'):
                        subprocess.Popen(['x-terminal-emulator', '-e', str(script)])
                    else:
                        script.unlink()
                        raise ValueError('No supported terminal found (macOS Terminal or Linux x-terminal-emulator required)')
                finally:
                    launches.release()
                self.reply(200, {'launched': True})
            except (ValueError, TypeError, OSError, subprocess.SubprocessError) as error:
                self.reply(400, {'error': str(error)})

    server = ThreadingHTTPServer(('127.0.0.1', 7334), Handler)
    link = f'{origin}/#connect?launcher_port={server.server_port}&launcher_key={secret}'
    print(f'Raven launcher allows Claude Code and Codex terminals in {project}. Ctrl-C stops it.')
    webbrowser.open(link)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    main()
