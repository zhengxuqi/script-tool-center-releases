"""Stable JSON-lines bridge between a tool process and the local hub."""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

DEFAULT_BROWSER_PROFILE = r'D:\ChromeDebug\User Data'


def ensure_browser_directory(profile=None):
    value = str(profile).strip() if profile is not None else ''
    path = Path(value or DEFAULT_BROWSER_PROFILE).expanduser()
    if not path.is_absolute():
        raise ValueError('请填写浏览器用户目录的完整路径。')
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError(f'无法创建浏览器用户目录：{path}。请检查盘符和写入权限，或在偏好设置中选择其他目录。') from None
    return path.resolve()


def seed_defaults(source, data):
    defaults = Path(source) / 'defaults'
    if defaults.is_dir():
        for path in defaults.rglob('*'):
            if path.is_file() and not path.is_symlink():
                target = Path(data) / path.relative_to(defaults)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Context:
    def __init__(self, request):
        self.request = request
        self.params = request.get('params', {})
        self.data = Path(request['data_dir'])
        self.output = Path(request['output_dir'])
        self.data.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        seed_defaults(Path(sys.argv[0]).resolve().parent, self.data)
        self.stop = threading.Event()
        self._done = threading.Event()
        self._write_lock = threading.Lock()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        import psutil
        parent = psutil.Process(os.getppid())
        parent_time = parent.create_time()
        while not self._done.wait(.5):
            if Path(self.request['stop_file']).exists():
                self.stop.set()
            try:
                if not parent.is_running() or parent.create_time() != parent_time:
                    self.stop.set()
            except psutil.Error:
                self.stop.set()

    def emit(self, kind, **values):
        with self._write_lock:
            print(json.dumps({'type': kind, **values}, ensure_ascii=False), flush=True)

    def log(self, message):
        self.emit('log', message=str(message))

    def progress(self, completed, total):
        self.emit('progress', completed=completed, total=total)

    def verify_browser(self):
        """A reused debug port must belong to the selected Chrome profile."""
        import psutil
        profile = ensure_browser_directory(self.request['browser'].get('profile'))
        port = int(self.request['browser']['port'])
        for connection in psutil.net_connections(kind='tcp'):
            if connection.status != psutil.CONN_LISTEN or connection.laddr.port != port:
                continue
            try:
                args = psutil.Process(connection.pid).cmdline()
                candidate = next((v.split('=', 1)[1] for v in args if v.startswith('--user-data-dir=')), '')
                if not candidate or Path(candidate).resolve() != profile:
                    raise ValueError('浏览器连接端口已被其他用户目录占用，请在设置中选择正确目录或端口。')
            except psutil.Error:
                raise ValueError('无法确认浏览器使用的用户目录，请检查设置。') from None


def run(main, health_check):
    if '--health-check' in sys.argv:
        health_check()
        print('ok')
        return
    request_path = Path(sys.argv[sys.argv.index('--request') + 1])
    context = Context(json.loads(request_path.read_text(encoding='utf-8')))
    try:
        result = main(context)
        context.emit('result', value=result or {})
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, OSError)) else '操作未完成，请检查浏览器登录状态、网络或文件占用。'
        context.emit('error', message=message[:1500])
        raise SystemExit(1) from None
    finally:
        context._done.set()
