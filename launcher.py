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


def fallback_compatibility(installed, version):
    """Use only trusted manifests and stdlib so recovery also works when app imports fail."""
    def numbers(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
            raise ValueError('版本号无效')
        return tuple(map(int, value.split('.')))
    problems = []
    try:
        target = numbers(version)
        for identifier, record in installed.items():
            if identifier == 'hub':
                continue
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', identifier):
                raise ValueError('工具编号无效')
            current = record['current']
            numbers(current)
            manifest = json.loads((ROOT / 'tools' / identifier / 'versions' / current / 'tool.json').read_text(encoding='utf-8-sig'))
            if not isinstance(manifest, dict) or manifest.get('id') != identifier or manifest.get('version') != current:
                raise ValueError('工具清单与版本记录不一致')
            minimum = manifest.get('min_hub_version', '1.0.0')
            if numbers(minimum) > target:
                problems.append(f'{manifest.get("name", identifier)} {current} 需要工具中心 {minimum}')
    except (OSError, ValueError, KeyError, TypeError):
        problems.append('无法确认已安装工具的兼容性，请检查工具版本文件是否完整')
    return problems


def save_registry(path, installed):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def require_safe_fallback(installed, previous):
    problems = fallback_compatibility(installed, previous)
    if problems:
        message = ('新版本启动失败，无法安全回退到 ' + str(previous) + '：' + '；'.join(problems)
                   + '。已保留当前版本记录和个人数据，请使用完整安装包或对应源码修复。')
        (ROOT / 'state/startup-repair.json').write_text(json.dumps({
            'reason': message, 'installed': installed, 'fallback': previous}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise RuntimeError(message)


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
        require_safe_fallback(installed, previous)
        original = json.loads(json.dumps(installed))
        installed['hub'] = {'current': previous, 'previous': active}
        save_registry(config_path, installed)
        process = start(previous, args.port)
        for _ in range(60):
            value = health(args.port)
            if value and value['version'] == previous:
                ready = True
                break
            time.sleep(.25)
        if not ready:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            save_registry(config_path, original)
            raise RuntimeError('启动与回退均未成功，请查看 state/hub.log。')
        (ROOT / 'state/startup-rollback.txt').write_text('新版本启动失败，工具中心已自动恢复上一版本。', encoding='utf-8')
    else:
        (ROOT / 'state/startup-repair.json').unlink(missing_ok=True)
        (ROOT / 'state/startup-rollback.txt').unlink(missing_ok=True)
    if not args.no_open:
        webbrowser.open(f'http://127.0.0.1:{args.port}')


if __name__ == '__main__':
    main()
