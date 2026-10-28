"""Stable launcher. The web application itself lives in versioned directories."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def initialize_installation():
    """Source checkouts start at the tracked baseline, without tracking private state."""
    destination = ROOT / 'state/installed.json'
    if destination.exists():
        return
    baseline = ROOT / 'initial-versions.json'
    installed = json.loads(baseline.read_text(encoding='utf-8-sig')) if baseline.is_file() else {'hub': {'current': '1.0.0', 'previous': None}}
    if not isinstance(installed, dict) or 'hub' not in installed:
        raise ValueError('初始版本清单无效。')
    for identifier, record in installed.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', identifier) or not isinstance(record, dict) or not re.fullmatch(r'\d+\.\d+\.\d+', str(record.get('current', ''))):
            raise ValueError('初始版本清单无效。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, destination)


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
            value = json.load(response)
            return value if value.get('app') == 'script-tool-center' else None
    except Exception:
        return None


def start(version, port):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('工具中心版本号无效。')
    python = ROOT / f'runtime/envs/hub/{version}/Scripts/pythonw.exe'
    if not python.is_file():
        python = ROOT / '.venv/Scripts/pythonw.exe'
    if not python.is_file():
        python = Path(sys.executable)
    entry = ROOT / f'app/versions/{version}/hub.py'
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    log = ROOT / 'state/hub.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as handle:
        return subprocess.Popen([str(python), '-X', 'utf8', str(entry), '--port', str(port)],
                                stdout=handle, stderr=handle, cwd=ROOT, creationflags=flags)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18760)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--wait-pid', type=int)
    parser.add_argument('--prepare', action='store_true', help='安装当前版本所需的工具中心组件')
    args = parser.parse_args()
    initialize_installation()
    if args.prepare:
        installed_path = ROOT / 'state/installed.json'
        installed = json.loads(installed_path.read_text(encoding='utf-8')) if installed_path.exists() else {'hub': {'current': '1.0.0'}}
        version = installed['hub']['current']
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('工具中心版本号无效。')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-i', 'https://pypi.org/simple',
                               '-r', str(ROOT / f'app/versions/{version}/requirements.txt')])
        return
    if args.wait_pid:
        import psutil
        try:
            psutil.Process(args.wait_pid).wait(timeout=45)
        except psutil.NoSuchProcess:
            pass
    if health(args.port):
        if not args.no_open:
            webbrowser.open(f'http://127.0.0.1:{args.port}')
        return
    config_path = ROOT / 'state/installed.json'
    installed = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {'hub': {'current': '1.0.0'}}
    active = installed['hub']['current']
    process = start(active, args.port)
    ready = False
    for _ in range(60):
        value = health(args.port)
        if value and value['version'] == active:
            ready = True
            break
        if process.poll() is not None:
            break
        time.sleep(.25)
    if not ready:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        previous = installed['hub'].get('previous')
        if not previous:
            raise RuntimeError('工具中心启动失败，请查看 state/hub.log。')
        installed['hub'] = {'current': previous, 'previous': active}
        temporary = config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, config_path)
        process = start(previous, args.port)
        for _ in range(60):
            value = health(args.port)
            if value and value['version'] == previous:
                ready = True
                break
            time.sleep(.25)
        if not ready:
            raise RuntimeError('启动与回退均未成功，请查看 state/hub.log。')
        (ROOT / 'state/startup-rollback.txt').write_text('新版本启动失败，工具中心已自动恢复上一版本。', encoding='utf-8')
    if not args.no_open:
        webbrowser.open(f'http://127.0.0.1:{args.port}')


if __name__ == '__main__':
    main()
