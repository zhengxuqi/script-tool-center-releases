"""Own one isolated Node service and proxy its original UI through the hub."""
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .runtime import HIDDEN
from .shared_browser import ensure_shared_browser
from tool_sdk import atomic_json


class PDDPortal:
    PREFIX = '/tools/pdd-review'

    def __init__(self, store, tasks, token, auto_browser=True):
        self.store, self.tasks, self.token = store, tasks, token
        self.auto_browser = auto_browser
        self.lock = threading.RLock()
        self.process = None
        self.port = None
        self.service_token = None
        self.version = None
        self.browser_settings = None
        self.starting = False
        self.error = ''
        self.suspended = False
        self.http = requests.Session()
        self.http.trust_env = False  # Local private transport must never use a system proxy.

    def enabled(self):
        return 'pdd-review' in self.store.installed()

    def source(self):
        if not self.enabled():
            raise ValueError('拼多多评论工具尚未安装。')
        return self.store.version_path('pdd-review')

    def _request(self, method, path, data=None, timeout=12):
        return self.http.request(method, f'http://127.0.0.1:{self.port}{path}', data=data,
            headers={'X-Tool-Service-Token': self.service_token, 'Content-Type': 'application/json'},
            timeout=timeout, allow_redirects=False)

    def _prepare(self):
        source = self.source()
        node = shutil.which('node')
        if not node:
            raise ValueError('请先安装 Node.js 22 或以上版本，再打开拼多多工具。')
        check = subprocess.run([node, '-e', 'if(Number(process.versions.node.split(".")[0])<22)process.exit(1);require("node:sqlite")'],
            capture_output=True, timeout=15, creationflags=HIDDEN)
        if check.returncode:
            raise ValueError('当前 Node.js 版本不能运行拼多多工具，需要 22 或以上版本并支持 SQLite。')
        home = self.store.root / 'runtime/node-envs/pdd-review' / source.name
        home.mkdir(parents=True, exist_ok=True)
        # Only known distributable files are copied; state and credentials never enter a runtime version.
        files = [source / 'package.json', source / 'package-lock.json']
        for directory in ('src', 'public', 'test'):
            files.extend(sorted((source / directory).rglob('*.js')))
            files.extend(sorted((source / directory).rglob('*.html')))
            files.extend(sorted((source / directory).rglob('*.css')))
        digest = hashlib.sha256(b''.join(p.relative_to(source).as_posix().encode() + p.read_bytes() for p in files)).hexdigest()
        marker = home / 'ready.json'
        ready = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
        for file in files:
            target = home / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
        lock_hash = hashlib.sha256((source / 'package-lock.json').read_bytes()).hexdigest()
        if ready.get('lock_sha256') != lock_hash or not (home / 'node_modules/exceljs/package.json').exists() or not (home / 'node_modules/playwright-core/package.json').exists():
            npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
            cli = Path(npm).resolve().parent / 'node_modules/npm/bin/npm-cli.js' if npm else None
            if not cli or not cli.is_file():
                raise ValueError('未找到 Node.js 附带的 npm，请完整安装 Node.js 后重试。')
            try:
                result = subprocess.run([node, str(cli), 'ci', '--omit=dev', '--ignore-scripts',
                    '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org'],
                    cwd=home, capture_output=True, timeout=180, creationflags=HIDDEN)
            except subprocess.TimeoutExpired:
                raise ValueError('拼多多组件下载超时，请检查网络后重试。') from None
            if result.returncode:
                raise ValueError('拼多多组件安装失败，请检查 npm 下载网络后重试。')
        probe = subprocess.run([node, '-e', 'Promise.all([import("exceljs"),import("playwright-core")]).catch(()=>process.exit(1))'],
                               cwd=home, capture_output=True, timeout=20, creationflags=HIDDEN)
        if probe.returncode:
            raise ValueError('拼多多运行组件不完整，请重新安装该工具版本。')
        atomic_json(marker, {'lock_sha256': lock_hash, 'source_sha256': digest})
        return node, home

    def _ensure(self):
        if self.suspended:
            raise ValueError('拼多多服务已停止，请点击“重新打开采集页”。')
        if self.process and self.process.poll() is None:
            selected_browser = (self.store.config['profile'], self.store.config['port'])
            if self.version != self.source().name or self.browser_settings != selected_browser:
                self.stop()
                self.suspended = False
            else:
                return
        if self.tasks.maintenance or self.tasks.closing:
            raise ValueError('正在更新或退出，请稍后打开拼多多工具。')
        self.starting, self.error = True, ''
        try:
            if self.auto_browser and self.store.unfinished():
                raise ValueError('请先等待当前任务队列结束，再打开拼多多共享浏览器。')
            node, home = self._prepare()
            selected_browser = (self.store.config['profile'], self.store.config['port'])
            endpoint = (ensure_shared_browser(*selected_browser) if self.auto_browser
                        else f'http://127.0.0.1:{selected_browser[1]}')
            with socket.socket() as candidate:
                candidate.bind(('127.0.0.1', 0))
                self.port = candidate.getsockname()[1]
            self.service_token = secrets.token_urlsafe(32)
            data = self.store.state / 'tools/pdd-review'
            data.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.update(PDD_DATA_DIR=str(data), PDD_HUB_TOKEN=self.service_token,
                       COLLECTOR_PORT=str(self.port), PDD_NO_AUTO_BROWSER='0' if self.auto_browser else '1',
                       PDD_BROWSER_ENDPOINT=endpoint, PDD_BROWSER_PROFILE=selected_browser[0])
            self.process = subprocess.Popen([node, str(home / 'src/server.js')], cwd=home, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=HIDDEN)
            self.version = self.source().name
            self.browser_settings = selected_browser
            for _ in range(100):
                if self.process.poll() is not None:
                    raise ValueError('拼多多服务未能启动，请检查 Node.js 和本地数据库。')
                try:
                    if self._request('GET', '/api/status', timeout=.4).status_code == 200:
                        return
                except requests.RequestException:
                    pass
                time.sleep(.1)
            raise ValueError('拼多多服务启动超时，请稍后重试。')
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.error = str(exc) if isinstance(exc, ValueError) else '拼多多服务准备失败，请检查本机运行环境。'
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            raise ValueError(self.error) from None
        finally:
            self.starting = False

    def info(self):
        if not self.lock.acquire(blocking=False):
            return {'running': bool(self.process and self.process.poll() is None), 'starting': self.starting}
        try:
            running = bool(self.process and self.process.poll() is None)
            result = {'running': running, 'starting': self.starting, 'error': self.error}
            if running:
                try:
                    state = self._request('GET', '/api/status', timeout=1).json()
                    result.update(busy=bool(state['browserBusy']), browser_connected=bool(state['browserConnected']))
                except (requests.RequestException, ValueError, KeyError):
                    result.update(busy=True, error='拼多多服务暂未响应，请在采集页面确认状态。')
            return result
        finally:
            self.lock.release()

    def forward(self, method, path, body=None):
        target = urlsplit(path)
        allowed = {'/api/status', '/api/reviews', '/api/settings', '/api/browser/open',
                   '/api/capture-current', '/api/products', '/api/export.xlsx'}
        route = target.path.removeprefix(self.PREFIX)
        if route not in allowed:
            raise ValueError('不支持的拼多多操作。')
        with self.tasks.gate, self.lock:
            if method == 'POST' and route in {'/api/browser/open', '/api/capture-current'} and self.store.unfinished():
                raise ValueError('其他工具已在采集或排队，请等待队列结束后再操作拼多多。')
            self._ensure()
            if method == 'POST' and route == '/api/browser/open' and self.auto_browser:
                ensure_shared_browser(self.store.config['profile'], self.store.config['port'])
            try:
                response = self._request(method, route + ('?' + target.query if target.query else ''), body, timeout=60)
            except requests.RequestException:
                raise ValueError('拼多多服务暂未响应，请稍后重试。') from None
            if method == 'GET' and route == '/api/export.xlsx' and response.status_code == 200 and response.content.startswith(b'PK'):
                self.store.save_export('pdd-review', '拼多多评论.xlsx', response.content)
            return response

    def stop(self, force=False):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.suspended = True
                return
            if not force and self.info().get('busy'):
                raise ValueError('拼多多正在采集，请等待采集结束后再停止或更新。')
            try:
                response = self._request('POST', '/api/shutdown', b'{}', timeout=3)
                response.raise_for_status()
                self.process.wait(timeout=12)
            except (requests.RequestException, subprocess.TimeoutExpired):
                if not force:
                    raise ValueError('拼多多服务尚未停止，请稍后重试。') from None
                self.process.terminate()
                self.process.wait(timeout=10)
            self.suspended = True

    def html(self):
        with self.tasks.gate, self.lock:
            if self.tasks.maintenance or self.tasks.closing:
                raise ValueError('正在更新或退出，请稍后打开拼多多工具。')
            self.suspended = False
        text = (self.source() / 'public/index.html').read_text(encoding='utf-8')
        text = text.replace('href="/styles.css"', 'href="/tools/pdd-review/styles.css"')
        text = text.replace('src="/app.js"', 'src="/tools/pdd-review/app.js"')
        text = text.replace('</head>', '<meta name="local-token" content="' + self.token + '">'
            '<link rel="stylesheet" href="/tools/pdd-review/portal-theme.css"></head>')
        return text.encode('utf-8')

    def javascript(self):
        text = (self.source() / 'public/app.js').read_text(encoding='utf-8')
        text = 'const TOOL_CENTER_TOKEN=document.querySelector(\'meta[name="local-token"]\').content;\n' + text
        text = text.replace('fetch(path, options)',
            'fetch(path, {...options, headers:{...options?.headers,"X-Local-Token":TOOL_CENTER_TOKEN}})')
        text = text.replace('/api/', self.PREFIX + '/api/')
        text = text.replace('${params}`', '${params}&_token=${encodeURIComponent(TOOL_CENTER_TOKEN)}`')
        text += '\nnew ResizeObserver(()=>{const shell=document.querySelector(".shell");'
        text += 'window.parent.postMessage({kind:"pdd-frame-size",height:Math.ceil(shell.getBoundingClientRect().height)+8},location.origin);}).observe(document.querySelector(".shell"));\n'
        return text.encode('utf-8')
