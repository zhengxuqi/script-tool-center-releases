"""A local web workspace for personally authenticated Windows scripts."""
import argparse
import hmac
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from hubcore.storage import Store, read_json
from hubcore.runtime import Environments, TaskQueue, HIDDEN
from hubcore.updates import Updates, MAX_ARCHIVE
from tool_sdk import atomic_json, seed_defaults

APP = Path(__file__).resolve().parent
ROOT = APP.parents[2]
VERSION = read_json(APP / 'tool.json')['version']


class Application:
    def __init__(self, root=ROOT):
        self.store = Store(root)
        for tool in self.store.tools():
            seed_defaults(self.store.version_path(tool['id']), self.store.state / 'tools' / tool['id'])
        self.environments = Environments(root)
        self.tasks = TaskQueue(self.store, self.environments)
        self.updates = Updates(self.store, self.environments, self.tasks, VERSION)
        self.token = secrets.token_urlsafe(32)

    def snapshot(self):
        return {'version': VERSION, 'config': self.store.config, 'tools': self.store.tools(),
                'installed': self.store.installed(), 'jobs': self.store.jobs(),
                'updates': self.updates.snapshot(), 'outputs': str(self.store.outputs),
                'startup_notice': (self.store.state / 'startup-rollback.txt').read_text(encoding='utf-8') if (self.store.state / 'startup-rollback.txt').is_file() else '',
                'public_key_ready': self.updates.public_key.is_file()}


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalToolCenter'

    @property
    def app(self):
        return self.server.app

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def log_message(self, *args):
        pass

    def valid_host(self):
        return self.headers.get('Host') == urlsplit(self.origin).netloc

    def authorized(self, write=False):
        if not self.valid_host():
            return False
        if write and self.headers.get('Origin') != self.origin:
            return False
        return hmac.compare_digest(self.headers.get('X-Local-Token', ''), self.app.token)

    def send_bytes(self, content, mime='application/json; charset=utf-8', code=200, disposition=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, value, code=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'), code=code)

    def do_GET(self):
        try:
            if not self.valid_host():
                return self.json({'error': '访问地址无效。'}, 403)
            parsed = urlsplit(self.path)
            route = parsed.path
            query = {key: rows[0] for key, rows in parse_qs(parsed.query).items()}
            if route == '/api/health':
                return self.json({'ok': True, 'app': 'script-tool-center', 'version': VERSION, 'pid': os.getpid()})
            if route in {'/', '/app.js', '/style.css'}:
                name = 'index.html' if route == '/' else route[1:]
                content = (APP / 'web' / name).read_bytes()
                if route == '/':
                    content = content.replace(b'__LOCAL_TOKEN__', self.app.token.encode())
                return self.send_bytes(content, mimetypes.guess_type(name)[0] + '; charset=utf-8')
            if not self.authorized():
                return self.json({'error': '页面会话已更新，请刷新页面。'}, 403)
            if route == '/api/state':
                return self.json(self.app.snapshot())
            if route == '/api/catalog':
                return self.json(self.app.store.catalog(query.get('tool', '')))
            if route == '/api/tool-settings':
                identifier = query.get('tool', '')
                self.app.store.tool(identifier)
                data = self.app.store.state / 'tools' / identifier
                return self.json({'settings': read_json(data / 'settings.json', {}),
                                  'limits': read_json(data / 'limits.json', None)})
            if route == '/api/results':
                return self.json(self.app.store.results())
            if route == '/api/download':
                target = (self.app.store.outputs / query.get('path', '')).resolve()
                if not target.is_relative_to(self.app.store.outputs) or not target.is_file() or target.suffix.lower() not in {'.xlsx', '.json'}:
                    raise ValueError('没有找到这份结果。')
                return self.send_bytes(target.read_bytes(), mimetypes.guess_type(target.name)[0] or 'application/octet-stream',
                                       disposition="attachment; filename*=UTF-8''" + quote(target.name))
            self.json({'error': '页面不存在。'}, 404)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.json({'error': str(exc) if isinstance(exc, ValueError) else '内容读取失败，请检查文件是否仍存在。'}, 400)

    def do_POST(self):
        try:
            if not self.authorized(write=True):
                return self.json({'error': '请求来源或页面会话无效，请刷新页面。'}, 403)
            route = urlsplit(self.path).path
            size = int(self.headers.get('Content-Length', '0'))
            maximum = MAX_ARCHIVE if route == '/api/updates/import' else 256 * 1024
            if size < 0 or size > maximum:
                return self.json({'error': '提交内容过大。'}, 413)
            content = self.rfile.read(size)
            if route == '/api/updates/import':
                self.app.updates.begin(self.app.updates.install, content, exclusive=True)
                return self.json({'ok': True})
            payload = json.loads(content or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('请求格式无效。')
            if route == '/api/settings':
                self.app.store.save_config(payload)
            elif route == '/api/submit':
                job = self.app.tasks.submit(payload['tool_id'], payload['action'], payload.get('params', {}), payload.get('resume'))
                return self.json({'ok': True, 'job_id': job['id']})
            elif route == '/api/stop':
                self.app.tasks.stop(payload['job_id'])
            elif route == '/api/tool-settings':
                identifier = payload['tool_id']
                self.app.store.tool(identifier)
                if not isinstance(payload['params'], dict):
                    raise ValueError('工具配置格式无效。')
                atomic_json(self.app.store.state / 'tools' / identifier / 'settings.json', payload['params'])
            elif route == '/api/updates/check':
                self.app.updates.begin(self.app.updates.check)
            elif route == '/api/updates/install':
                self.app.updates.begin(self.app.updates.download_install, payload['tool_id'], exclusive=True)
            elif route == '/api/updates/rollback':
                self.app.updates.begin(self.app.updates.rollback, payload['tool_id'], exclusive=True)
            elif route == '/api/reveal':
                os.startfile(self.app.store.outputs)
            elif route in {'/api/restart', '/api/shutdown'}:
                with self.app.tasks.gate:
                    if self.app.store.unfinished() or self.app.updates.snapshot()['running']:
                        raise ValueError('请先停止任务并等待更新结束，再退出或重启。')
                    self.app.tasks.closing = True
                self.json({'ok': True})
                if route == '/api/restart':
                    python = Path(sys.executable).with_name('pythonw.exe') if os.name == 'nt' else Path(sys.executable)
                    subprocess.Popen([str(python), str(ROOT / 'launcher.py'), '--wait-pid', str(os.getpid()),
                                      '--port', str(self.server.server_port), '--no-open'],
                                     cwd=ROOT, creationflags=HIDDEN)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self.json({'error': '不支持的操作。'}, 404)
            self.json({'ok': True})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.json({'error': str(exc) if isinstance(exc, ValueError) else '请求未完成，请检查输入内容。'}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18760)
    parser.add_argument('--health-check', action='store_true')
    args = parser.parse_args()
    if args.health_check:
        for file in ('web/index.html', 'web/app.js', 'web/style.css', 'tool.json'):
            if not (APP / file).is_file():
                raise SystemExit(1)
        print('ok')
        return
    app = Application()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    server.app = app
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
