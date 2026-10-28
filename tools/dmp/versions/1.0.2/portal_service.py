"""Host the unchanged DMP application under the tool center's shared queue.

The original HTML and backend are retained byte-for-byte. Integration lives here.
"""
import argparse
import copy
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import requests
import coverage
from tool_sdk import Context

DATA = Path(os.environ.get('TOOL_DATA_DIR', str(Path(__file__).parent / 'defaults')))
coverage.ROOT = DATA
import original_webapp as original
from category_catalog import CategoryCatalog


class ManagedApplication(original.LocalApplication):
    def __init__(self, data, outputs, submit):
        data = Path(data)
        super().__init__(outputs=outputs, catalog=CategoryCatalog(data / 'category_catalog.json'),
                         settings_path=data / 'ui-settings.json',
                         presets_path=data / 'data/category_presets.json',
                         price_presets_path=data / 'data/price_presets.json',
                         category_templates_path=data / 'data/category_template_assignments.json')
        self.submit_to_hub = submit
        self.entries = {}
        self.active_ticket = None
        self.submission = threading.local()

    def snapshot(self, catalog_revision=None):
        value = super().snapshot(catalog_revision)
        value['output_dir'] = os.environ.get('HUB_OUTPUTS', value['output_dir'])
        value['portal_ticket'] = self.active_ticket
        return value

    def begin(self, kind, action):
        with self.lock:
            if self.closing or self.clearing or self.browser_opening or self.task['running']:
                raise ValueError('当前有操作正在执行，请先等待完成或停止任务。')
            ticket = secrets.token_hex(16)
            browser = getattr(self.submission, 'browser', None) or {
                'profile': self.settings['user_data_dir'], 'port': self.settings['port']}
            self.stop_event.clear()
            self.task = {'running': True, 'kind': kind, 'status': '已加入工具中心队列，等待前面的任务完成',
                         'completed': 0, 'total': 0, 'logs': [], 'output': None, 'error': None}
            self.active_ticket = ticket
            self.entries[ticket] = {'phase': 'queued', 'kind': kind, 'action': action, 'browser': browser}
            try:
                self.submit_to_hub(ticket, kind, browser)
            except Exception:
                self.entries.pop(ticket, None)
                self.task.update(running=False, status='提交任务失败', error='工具中心暂时无法接收任务，请稍后重试。')
                raise ValueError(self.task['error']) from None

    def start(self, payload):
        self.submission.browser = {'profile': str(payload.get('user_data_dir', self.settings['user_data_dir'])),
                                   'port': int(payload.get('port', self.settings['port']))}
        return super().start(payload)

    def refresh(self, payload):
        self.submission.browser = {'profile': str(payload.get('user_data_dir', self.settings['user_data_dir'])),
                                   'port': int(payload.get('port', self.settings['port']))}
        return super().refresh(payload)

    def resume(self, name):
        if name != original.PROGRESS_NAME:
            raise ValueError('只能继续当前采集进度。')
        settings = original.load_progress(self.output_path(name, '.json'))['settings']
        self.submission.browser = {'profile': settings['user_data_dir'], 'port': settings['port']}
        return super().resume(name)

    def open_browser(self, payload):
        browser = {'profile': str(payload.get('user_data_dir', self.settings['user_data_dir'])),
                   'port': int(payload.get('port', self.settings['port']))}
        Context.verify_browser(SimpleNamespace(request={'browser': browser}))
        return super().open_browser(payload)

    def execute(self, ticket):
        with self.lock:
            entry = self.entries.get(ticket)
            if not entry or entry['phase'] != 'queued' or self.active_ticket != ticket:
                raise ValueError('任务已取消或不再属于当前页面，请重新提交。')
            self.stop_event.clear()
            self.task.update(status='执行中')
            entry['phase'] = 'running'
            def work():
                try:
                    Context.verify_browser(SimpleNamespace(request={'browser': entry['browser']}))
                    entry['action']()
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (ValueError, OSError)) else '任务执行失败，请检查浏览器、登录状态或网络后重试。'
                    self.log(message)
                    with self.lock:
                        self.task.update(status='已停止' if self.stop_event.is_set() else '失败', error=message)
                finally:
                    with self.lock:
                        self.task['running'] = False
                        entry['phase'] = 'finished'
                        entry['task'] = copy.deepcopy(self.task)
                        entry.pop('action', None)
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

    def cancel(self, ticket):
        with self.lock:
            entry = self.entries.get(ticket)
            if not entry or entry['phase'] == 'finished':
                return
            if self.active_ticket != ticket:
                return
            self.stop_event.set()
            if entry['phase'] == 'queued':
                self.task.update(running=False, status='已取消排队')
                entry.update(phase='finished', task=copy.deepcopy(self.task))
                entry.pop('action', None)
            else:
                self.log('已请求停止，正在保留已完成的结果。')

    def job(self, ticket):
        with self.lock:
            entry = self.entries.get(ticket)
            if not entry:
                raise ValueError('任务不存在，可能因服务重启中断；已保存进度可从原页面继续。')
            return {'phase': entry['phase'], 'task': copy.deepcopy(entry.get('task', self.task))}


class ManagedHandler(original.Handler):
    def managed_auth(self):
        return self.valid_host() and hmac.compare_digest(self.headers.get('X-Local-Token', ''), self.server.token)

    def do_GET(self):
        parts = urlsplit(self.path)
        if not parts.path.startswith('/managed/'):
            return super().do_GET()
        if not self.managed_auth():
            return self.send(403, {'error': '无效的工具中心会话。'})
        try:
            if parts.path == '/managed/health':
                return self.send(200, {'ok': True})
            if parts.path == '/managed/job':
                return self.send(200, self.server.app.job(parse_qs(parts.query).get('ticket', [''])[0]))
            self.send(404, {'error': '操作不存在。'})
        except ValueError as exc:
            self.send(400, {'error': str(exc)})

    def do_POST(self):
        if not urlsplit(self.path).path.startswith('/managed/'):
            return super().do_POST()
        if not self.managed_auth() or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}':
            return self.send(403, {'error': '无效的工具中心会话。'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('请求大小无效。')
            payload = json.loads(self.rfile.read(size))
            if self.path == '/managed/execute':
                self.server.app.execute(payload['ticket'])
            elif self.path == '/managed/cancel':
                self.server.app.cancel(payload['ticket'])
            elif self.path == '/managed/shutdown':
                if self.server.app.task['running'] or self.server.app.clearing:
                    raise ValueError('达摩盘仍有操作正在执行。')
                self.server.app.closing = True
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                return self.send(404, {'error': '操作不存在。'})
            self.send(200, {'ok': True})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error': str(exc)})


class ManagedServer(original.LocalServer):
    def __init__(self, address, app, token):
        self.app, self.token = app, token
        self.pages = original.PageSessions()
        ThreadingHTTPServer.__init__(self, address, ManagedHandler)

    def service_actions(self):
        # The hub, rather than navigation between its tools, owns process lifetime.
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    hub = os.environ['HUB_URL']
    def submit(ticket, kind, browser):
        response = requests.post(hub + '/api/internal/dmp-submit', json={
            'ticket': ticket, 'kind': kind, 'browser': browser}, headers={
            'X-Local-Token': os.environ['HUB_LOCAL_TOKEN'], 'Origin': hub}, timeout=15)
        if response.status_code != 200:
            raise ValueError('工具中心暂时无法接收任务。')
    legacy = Path(os.environ['HUB_OUTPUTS']) / 'dmp/original'
    workspace = Path(os.environ.get('TOOL_OUTPUTS_DIR', str(DATA / 'workspace')))
    workspace.mkdir(parents=True, exist_ok=True)
    # Copy the previous checkpoint and current files once; keep originals intact for old links.
    if legacy.is_dir():
        for source in legacy.rglob('*'):
            if source.is_file() and not source.is_symlink() and source.suffix.lower() in {'.xlsx', '.json', '.tmp'}:
                destination = workspace / source.relative_to(legacy)
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    app = ManagedApplication(DATA, workspace, submit)
    server = ManagedServer(('127.0.0.1', args.port), app, os.environ['DMP_SERVICE_TOKEN'])
    def watch_parent():
        import psutil
        try:
            parent = psutil.Process(int(os.environ['HUB_PARENT_PID']))
        except psutil.Error:
            app.stop_event.set()
            app.closing = True
            server.shutdown()
            return
        while not app.closing:
            if not parent.is_running():
                app.stop_event.set()
                app.closing = True
                server.shutdown()
                return
            time.sleep(1)
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        app.closing = True
        app.stop_event.set()
        server.server_close()
        if app.worker and app.worker.is_alive():
            app.worker.join(timeout=75)


if __name__ == '__main__':
    main()
