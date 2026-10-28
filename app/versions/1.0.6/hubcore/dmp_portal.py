"""Mount the original DMP page and its isolated backend in the local hub."""
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from .runtime import HIDDEN, child_environment


class DMPPortal:
    def __init__(self, store, environments, tasks, token):
        self.store, self.environments, self.tasks, self.token = store, environments, tasks, token
        self.hub_url = None
        self.process = None
        self.url = None
        self.secret = None
        self.version = None
        self.lock = threading.Lock()

    def enabled(self):
        return self.store.tool('dmp').get('ui') == 'original'

    def html(self):
        source = self.store.version_path('dmp')
        html = (source / 'web/index.html').read_text(encoding='utf-8')
        html = html.replace('__LOCAL_TOKEN__', self.token)
        html = html.replace("'/api/download?file='", "'/tools/dmp/api/download?_token=" + self.token + "&file='")
        html = html.replace("'/api/", "'/tools/dmp/api/")
        html = html.replace("headers:data===undefined?{}:", "headers:data===undefined?{'X-Local-Token':TOKEN}:")
        html = html.replace('关闭最后一个采集页面后自动停止任务并退出程序。', '任务由工具中心管理，切换页面后可在任务中心查看。')
        theme = (source / 'web/portal-theme.css').read_text(encoding='utf-8')
        # The original page defines a function named parent(). Capture the native
        # owner window before that script declares its globals.
        html = html.replace('</head>', '<style>' + theme + '</style><script>const TOOL_CENTER_FRAME_OWNER=window.parent;</script></head>')
        bridge = """<script>
const reportHeight=()=>TOOL_CENTER_FRAME_OWNER.postMessage({kind:'dmp-frame-size',height:Math.ceil(document.body.getBoundingClientRect().height)+12},location.origin);
new ResizeObserver(reportHeight).observe(document.body);window.addEventListener('load',reportHeight);reportHeight();
document.documentElement.style.setProperty('--tool-center-review-height',Math.round(TOOL_CENTER_FRAME_OWNER.innerHeight*.55)+'px');
document.documentElement.style.setProperty('--tool-center-dialog-height',Math.max(200,TOOL_CENTER_FRAME_OWNER.innerHeight-50)+'px');
let hubActiveDialog=null;
new MutationObserver(()=>{const dialog=document.querySelector('dialog[open]');if(dialog===hubActiveDialog)return;hubActiveDialog=dialog;if(dialog){const rect=dialog.getBoundingClientRect();TOOL_CENTER_FRAME_OWNER.postMessage({kind:'dmp-dialog-open',top:rect.top,height:rect.height},location.origin);}else TOOL_CENTER_FRAME_OWNER.postMessage({kind:'dmp-dialog-closed'},location.origin);}).observe(document.body,{attributes:true,subtree:true,attributeFilter:['open']});
</script>"""
        return html.replace('</body>', bridge + '</body>').encode('utf-8')

    def _headers(self):
        return {'X-Local-Token': self.secret, 'Origin': self.url, 'Content-Type': 'application/json'}

    def ensure(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError('达摩盘正在准备运行环境，请稍候。')
        try:
            version = self.store.tool('dmp')['version']
            if self.process is not None and self.process.poll() is None:
                if version == self.version:
                    return
                self._close()
            if not self.hub_url:
                raise ValueError('请先从工具中心打开达摩盘页面。')
            source = self.store.version_path('dmp', version)
            python = self.environments.ensure('dmp', version, source, lambda message: None,
                                              cancelled=lambda: self.tasks.closing)
            with socket.socket() as connection:
                connection.bind(('127.0.0.1', 0))
                port = connection.getsockname()[1]
            self.secret = secrets.token_urlsafe(32)
            self.url, self.version = f'http://127.0.0.1:{port}', version
            environment = child_environment()
            environment.update(TOOL_DATA_DIR=str(self.store.state / 'tools/dmp'), HUB_OUTPUTS=str(self.store.outputs),
                               TOOL_OUTPUTS_DIR=str(self.store.state / 'tools/dmp/workspace'),
                               DMP_USER_DATA_DIR=str(self.store.config['profile']),
                               HUB_URL=self.hub_url, HUB_LOCAL_TOKEN=self.token, DMP_SERVICE_TOKEN=self.secret,
                               HUB_PARENT_PID=str(os.getpid()))
            self.process = subprocess.Popen([str(python), '-X', 'utf8', str(source / 'portal_service.py'), '--port', str(port)],
                    cwd=source, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ValueError('达摩盘页面服务启动失败，请检查运行组件。')
                try:
                    response = requests.get(self.url + '/managed/health', headers=self._headers(), timeout=1)
                    if response.status_code == 200:
                        return
                except requests.RequestException:
                    pass
                time.sleep(.15)
            raise ValueError('达摩盘页面服务启动超时，请重试。')
        finally:
            self.lock.release()

    def call(self, route, payload=None, ensure=True):
        if ensure:
            self.ensure()
        try:
            response = requests.request('GET' if payload is None else 'POST', self.url + route,
                                        json=payload, headers=self._headers(), timeout=(5, 60))
        except requests.RequestException:
            raise ValueError('达摩盘页面服务连接中断，已保存的设置和进度保留。') from None
        return response

    def json(self, route, payload=None):
        response = self.call(route, payload)
        value = response.json()
        if not response.ok:
            raise ValueError(value.get('error', '达摩盘操作未完成。'))
        return value

    def submit(self, payload):
        if not self.enabled() or self.process is None or self.process.poll() is not None:
            raise ValueError('请先打开达摩盘工具页面。')
        ticket, kind = payload.get('ticket'), payload.get('kind')
        if not isinstance(ticket, str) or len(ticket) != 32 or any(c not in '0123456789abcdef' for c in ticket):
            raise ValueError('达摩盘任务编号无效。')
        if kind not in {'coverage', 'categories'}:
            raise ValueError('不支持的达摩盘任务。')
        browser = payload.get('browser', {})
        if not isinstance(browser.get('profile'), str) or not 1 <= int(browser.get('port', 0)) <= 65535:
            raise ValueError('浏览器设置无效。')
        with self.tasks.gate:
            config = self.store.config
            if (Path(browser['profile']).expanduser().resolve() != Path(config['profile']).expanduser().resolve()
                    or int(browser['port']) != int(config['port'])):
                raise ValueError('平台浏览器设置已改变，请刷新达摩盘页面后重新提交。')
            return self.tasks.submit('dmp', 'collect' if kind == 'coverage' else 'categories', {
                '_portal_ticket': ticket, '_portal_browser': {'profile': config['profile'], 'port': config['port']}})

    def forward(self, path, payload=None):
        route = path.removeprefix('/tools/dmp')
        if urlsplit(route).path == '/api/browser/open':
            self.tasks.reserve_update()
            try:
                return self.call(route, payload)
            finally:
                self.tasks.release_update()
        if urlsplit(route).path in {'/api/stop', '/api/clear'}:
            current = self.json('/api/state').get('portal_ticket')
            for job in self.store.unfinished():
                if job['tool_id'] == 'dmp' and job['params'].get('_portal_ticket') == current:
                    self.tasks.stop(job['id'])
        return self.call(route, payload)

    def cancel(self, job):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.call('/managed/cancel', {'ticket': job['params']['_portal_ticket']}, ensure=False)
        except ValueError:
            pass

    def run_job(self, job):
        ticket = job['params']['_portal_ticket']
        result = self.json('/managed/execute', {'ticket': ticket})
        stopping = False
        while True:
            latest = self.store.job(job['id'])
            if latest['status'] == 'stopping' and not stopping:
                stopping = True
                self.cancel(job)
            snapshot = self.json('/managed/job?ticket=' + quote(ticket))
            task = snapshot['task']
            with self.tasks.gate:
                latest = self.store.job(job['id'])
                latest.update(message=task['status'], logs=task.get('logs', [])[-150:],
                              completed=task.get('completed', 0), total=task.get('total', 0))
                if snapshot['phase'] == 'finished':
                    cancelled = latest['status'] == 'stopping'
                    published = []
                    if job['action'] == 'collect':
                        workspace = (self.store.state / 'tools/dmp/workspace').resolve()
                        for name in (task.get('output'), '覆盖人数.progress.json'):
                            if not name:
                                continue
                            path = (workspace / name).resolve()
                            if path.is_relative_to(workspace) and path.is_file() and not path.is_symlink():
                                published.append(self.store.save_export('dmp', path.name, path.read_bytes()))
                    latest.update(status='cancelled' if cancelled else 'failed' if task.get('error') else 'completed',
                                  finished=time.time(), result={'filename': published[0] if published else None,
                                      'files': published, 'original_ui': True})
                self.store.put_job(latest)
            if snapshot['phase'] == 'finished':
                return
            time.sleep(.4)

    def _close(self):
        if self.process is not None and self.process.poll() is None:
            response = self.call('/managed/shutdown', {}, ensure=False)
            if not response.ok:
                raise ValueError('达摩盘仍在执行操作，请等待结束后再更新或退出。')
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise ValueError('达摩盘页面服务仍在结束，请稍候。') from None
        self.process = None

    def close(self):
        with self.lock:
            self._close()
