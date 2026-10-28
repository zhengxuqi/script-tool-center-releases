"""Build signed Releases assets without bundling cookies or user data."""
import argparse
import base64
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / 'app/versions/1.0.0'))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hubcore.storage import read_json, version_tuple, IDENTIFIER
from hubcore.updates import canonical
from hubcore.updates import verify

KEY_HOME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ScriptToolCenterPublisher'


def sign(manifest, private_key):
    return {'manifest': manifest, 'signature': base64.b64encode(private_key.sign(canonical(manifest))).decode('ascii')}


def initialize_key(private_path=KEY_HOME / 'signing-key.pem', public_path=ROOT / 'trust/release-public-key.pem'):
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        key = Ed25519PrivateKey.generate()
        private_path.parent.mkdir(parents=True, exist_ok=True)
        with private_path.open('xb') as handle:
            handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_path.exists() and public_path.read_bytes() != public:
        raise ValueError('发行密钥与现有公钥不一致，请使用原发行密钥。')
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public)
    return key


def build_package(source, identifier, version, private_key, destination, notes=''):
    version_tuple(version)
    source, destination = Path(source), Path(destination)
    payload = {}
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if not path.is_file() or path.is_symlink() or any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        # Source code and explicitly designated public defaults only.
        if path.suffix not in {'.py', '.html', '.css', '.js', '.json', '.txt'}:
            continue
        if relative.parts[0] in {'state', 'outputs', 'browser-profile', 'analysis', 'runtime', 'trust'}:
            continue
        payload[relative.as_posix()] = path.read_bytes()
    tool = json.loads(payload['tool.json'])
    tool.update(id=identifier, version=version)
    payload['tool.json'] = json.dumps(tool, ensure_ascii=False, indent=2).encode('utf-8')
    manifest = {'schema': 1, 'id': identifier, 'version': version,
                'min_hub_version': tool.get('min_hub_version', '1.0.0'), 'notes': notes,
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()}}
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'{identifier}-{version}.zip'
    temporary = output.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bundle.json', json.dumps(sign(manifest, private_key), ensure_ascii=False))
        for name, content in payload.items():
            archive.writestr('payload/' + name, content)
    os.replace(temporary, output)
    return {'id': identifier, 'name': tool.get('name', identifier), 'version': version, 'min_hub_version': manifest['min_hub_version'],
            'asset': output.name, 'size': output.stat().st_size, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'notes': notes}


def build_distribution(destination):
    """A clean first-install package with no .venv, login, task or result data."""
    installed = read_json(ROOT / 'state/installed.json', read_json(ROOT / 'initial-versions.json', {key:{'current':'1.0.0','previous':None} for key in ('hub','boundary','dmp')}))
    output = destination / f'script-tool-center-{installed["hub"]["current"]}-Windows.zip'
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('launcher.py','启动工具中心.cmd','README.md','trust/release-public-key.pem'):
            archive.write(ROOT / name, name)
        initial = {key: {'current': row['current'], 'previous': None} for key,row in installed.items()}
        archive.writestr('state/installed.json', json.dumps(initial, ensure_ascii=False))
        repository = read_json(ROOT / 'state/config.json', {}).get('repository', '')
        archive.writestr('release-source.json', json.dumps({'repository':repository}, ensure_ascii=False))
        for identifier, record in installed.items():
            source = ROOT / ('app' if identifier == 'hub' else f'tools/{identifier}') / 'versions' / record['current']
            for path in source.rglob('*'):
                if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and not path.name.startswith('.'):
                    archive.write(path, path.relative_to(ROOT).as_posix())
    return output


def main():
    parser = argparse.ArgumentParser(description='构建 GitHub Releases 签名发行包')
    parser.add_argument('command', choices=['build','init-key'])
    parser.add_argument('--tool', default='all', help='工具编号，或 all')
    parser.add_argument('--source', type=Path, help='新工具的源码目录，需要 --tool 与 --version')
    parser.add_argument('--version', help='新版本号，例如 1.0.1；不填写则沿用当前版本')
    parser.add_argument('--notes', default='')
    parser.add_argument('--destination', type=Path, default=ROOT / 'dist')
    parser.add_argument('--distribution', action='store_true', help='另生成首次安装包')
    args = parser.parse_args()
    if args.tool != 'all' and not IDENTIFIER.fullmatch(args.tool):
        parser.error('工具编号只允许小写英文、数字和中划线。')
    if args.source and (args.tool == 'all' or not args.version):
        parser.error('--source 需要指定 --tool 和 --version。')
    key = initialize_key()
    if args.command == 'init-key':
        print('发行公钥已配置。私钥单独保存在本机发布者目录。')
        return
    installed = read_json(ROOT / 'state/installed.json', read_json(ROOT / 'initial-versions.json', {key:{'current':'1.0.0'} for key in ('hub','boundary','dmp')}))
    previous_index = args.destination / 'updates.json'
    packages_by_id = {}
    if previous_index.exists():
        previous = verify(read_json(previous_index), ROOT / 'trust/release-public-key.pem')
        packages_by_id = {row['id']: row for row in previous.get('packages', [])}
    packages = []
    for identifier in (list(installed) if args.tool == 'all' else [args.tool]):
        current = installed.get(identifier, {}).get('current', args.version)
        source = args.source or ROOT / ('app' if identifier == 'hub' else f'tools/{identifier}') / 'versions' / current
        packages.append(build_package(source, identifier, args.version or current, key, args.destination, args.notes))
    for package in packages:
        packages_by_id[package['id']] = package
    (args.destination / 'updates.json').write_text(json.dumps(sign({'schema':1,'packages':list(packages_by_id.values())},key), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'已生成 {len(packages)} 个签名发行包与 updates.json：{args.destination}')
    if args.distribution:
        print(f'首次安装包：{build_distribution(args.destination)}')
    current_assets = {row['asset'] for row in packages_by_id.values()} | {'updates.json'}
    installer_name = f'script-tool-center-{installed["hub"]["current"]}-Windows.zip'
    if (args.destination / installer_name).is_file():
        current_assets.add(installer_name)
    checksums = [hashlib.sha256((args.destination / name).read_bytes()).hexdigest()+'  '+name
                 for name in sorted(current_assets)]
    (args.destination / 'SHA256SUMS.txt').write_text('\n'.join(checksums)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
