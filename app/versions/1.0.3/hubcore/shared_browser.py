"""Start or reuse the one configured Chrome. Never terminate another browser."""
import os
from pathlib import Path
import subprocess
import time

import psutil
import requests

from .runtime import HIDDEN


def argument(args, name):
    for index, value in enumerate(args):
        if value.startswith(name + '='):
            return value.split('=', 1)[1]
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return ''


def ensure_shared_browser(profile, port):
    profile = Path(profile).expanduser().resolve()
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('共享浏览器端口无效。')
    listener = None
    for conn in psutil.net_connections(kind='tcp'):
        if conn.status != psutil.CONN_LISTEN or conn.laddr.port != port:
            continue
        try:
            args = psutil.Process(conn.pid).cmdline()
            selected = argument(args, '--user-data-dir')
            if not selected or Path(selected).resolve() != profile or Path(args[0]).name.lower() not in ('chrome.exe', 'chrome'):
                raise ValueError('共享浏览器端口被其他程序或用户目录占用，请检查平台浏览器设置。')
        except psutil.Error:
            raise ValueError('无法确认共享浏览器的用户目录，请检查 Chrome 状态。') from None
        listener = conn.pid
    if listener is None:
        for process in psutil.process_iter(['name', 'cmdline']):
            args = process.info.get('cmdline') or []
            selected = argument(args, '--user-data-dir')
            if selected and Path(selected).resolve() == profile:
                raise ValueError('此用户目录已被另一个 Chrome 使用，但没有提供平台连接端口。请正常关闭该 Chrome 后重试。')
        candidates = [Path(os.environ.get(key, '')) / relative for key, relative in (
            ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe'),
            ('PROGRAMFILES(X86)', 'Google/Chrome/Application/chrome.exe'),
            ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe')) if os.environ.get(key)]
        executable = next((p for p in candidates if p.is_file()), None)
        if executable is None:
            raise ValueError('未找到 Google Chrome，请先安装后再运行工具。')
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([str(executable), f'--user-data-dir={profile}',
            f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', 'about:blank'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=HIDDEN)
    endpoint = f'http://127.0.0.1:{port}'
    with requests.Session() as http:
        http.trust_env = False
        for _ in range(80):
            try:
                response = http.get(endpoint + '/json/version', timeout=.3, allow_redirects=False)
                if response.status_code == 200 and response.json().get('webSocketDebuggerUrl', '').startswith('ws://'):
                    return endpoint
            except (requests.RequestException, ValueError):
                pass
            time.sleep(.1)
    raise ValueError('共享 Chrome 未能启动或连接，请检查平台浏览器目录和端口。')
