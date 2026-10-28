"""Signed, bounded release packages; immutable versions and atomic pointers."""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

import requests
from cryptography.hazmat.primitives import serialization
from tool_sdk import atomic_json
from .storage import IDENTIFIER, read_json, version_tuple

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def verify(envelope, public_key):
    try:
        key = serialization.load_pem_public_key(Path(public_key).read_bytes())
        key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(envelope['manifest']))
        return envelope['manifest']
    except Exception:
        raise ValueError('发布签名校验失败，请确认更新包来自配置的发布者。') from None


def safe_member(name):
    if not isinstance(name, str) or '\\' in name or '\x00' in name:
        raise ValueError('更新包包含无效路径。')
    path = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if (path.is_absolute() or not path.parts or name != path.as_posix()
            or any(part in {'.', '..'} or part.endswith((' ', '.')) or ':' in part
                   or part.split('.')[0].upper() in reserved for part in path.parts)):
        raise ValueError('更新包包含不安全的路径。')
    return path


def inspect_archive(content, public_key):
    if len(content) > MAX_ARCHIVE:
        raise ValueError('更新包超过 100 MB。')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [row.filename for row in infos]
            if len(infos) > 2000 or sum(row.file_size for row in infos) > MAX_EXPANDED:
                raise ValueError('更新包解压大小或文件数量超出限制。')
            if len(set(name.casefold() for name in names)) != len(names):
                raise ValueError('更新包包含重复文件。')
            for row in infos:
                safe_member(row.filename)
                if row.is_dir() or stat.S_ISLNK(row.external_attr >> 16):
                    raise ValueError('更新包不允许目录项或符号链接。')
            if 'bundle.json' not in names:
                raise ValueError('缺少签名清单。')
            if archive.getinfo('bundle.json').file_size > 512 * 1024:
                raise ValueError('发布清单过大。')
            envelope = json.loads(archive.read('bundle.json'))
            manifest = verify(envelope, public_key)
            identifier, version = manifest['id'], manifest['version']
            if not IDENTIFIER.fullmatch(identifier):
                raise ValueError('工具编号无效。')
            version_tuple(version)
            version_tuple(manifest['min_hub_version'])
            files = manifest['files']
            if not isinstance(files, dict) or not files or len(files) > 1999:
                raise ValueError('文件清单无效。')
            expected = {'bundle.json'} | {'payload/' + str(name) for name in files}
            if set(names) != expected:
                raise ValueError('更新包文件与发布清单不一致。')
            payload = {}
            for name, digest in files.items():
                safe_member(name)
                data = archive.read('payload/' + name)
                if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('更新包文件校验失败。')
                payload[name] = data
            tool = json.loads(payload['tool.json'])
            if tool.get('id') != identifier or tool.get('version') != version:
                raise ValueError('工具信息与发布清单不一致。')
            if tool.get('min_hub_version') != manifest['min_hub_version']:
                raise ValueError('最低工具中心版本与清单不一致。')
            if identifier != 'hub':
                actions = tool.get('actions')
                if not isinstance(actions, list) or 'collect' not in actions or len(actions) > 20 or any(not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', value) for value in actions):
                    raise ValueError('工具操作清单无效，必须包含 collect。')
                form = tool.get('form', {})
                if not isinstance(form, dict) or not isinstance(form.get('fields', []), list):
                    raise ValueError('工具参数表单无效。')
            required = {'hub.py', 'requirements.txt'} if identifier == 'hub' else {'adapter.py', 'requirements.txt'}
            if not required.issubset(payload):
                raise ValueError('更新包缺少入口或组件清单。')
            return manifest, payload
    except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError, UnicodeError):
        raise ValueError('更新包格式无效。') from None


class Updates:
    def __init__(self, store, environments, tasks, hub_version):
        self.store, self.environments, self.tasks = store, environments, tasks
        self.hub_version = hub_version
        self.public_key = store.root / 'trust/release-public-key.pem'
        self.lock = threading.RLock()
        self.state = {'running': False, 'message': '尚未检查更新', 'available': [], 'logs': [], 'error': None}
        self.release_assets = {}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def log(self, message):
        with self.lock:
            self.state['message'] = str(message)
            self.state['logs'] = (self.state['logs'] + [str(message)])[-50:]

    def begin(self, operation, *args, exclusive=False):
        with self.lock:
            if self.state['running']:
                raise ValueError('正在处理更新，请稍候。')
            if exclusive:
                self.tasks.reserve_update()
            self.state.update(running=True, error=None, logs=[], message='正在准备')
        def work():
            try:
                operation(*args)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ValueError, OSError)) else '更新失败，请检查网络或发布配置，当前版本保持可用。'
                with self.lock:
                    self.state.update(error=message, message=message)
            finally:
                if exclusive:
                    self.tasks.release_update()
                with self.lock:
                    self.state['running'] = False
        threading.Thread(target=work, daemon=True).start()

    def _fetch(self, url, maximum, asset=False):
        # Public Releases only. No stored GitHub access token is needed.
        allowed = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
        current = url
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Local-Script-Tool-Center/1.0',
                   'X-GitHub-Api-Version': '2026-03-10'}
        for _ in range(7):
            parsed = urllib.parse.urlsplit(current)
            if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password:
                raise ValueError('更新下载地址无效。')
            try:
                with requests.get(current, headers=headers, stream=True, timeout=(15, 60), allow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        current = urllib.parse.urljoin(current, response.headers.get('Location', ''))
                        continue
                    if response.status_code != 200:
                        if response.status_code == 404:
                            raise ValueError('未找到公开发布版本，请检查仓库名称与 Releases。')
                        raise ValueError(f'GitHub 返回 HTTP {response.status_code}，请稍后重试。')
                    chunks, count = [], 0
                    for chunk in response.iter_content(65536):
                        count += len(chunk)
                        if count > maximum:
                            raise ValueError('下载内容超过大小限制。')
                        chunks.append(chunk)
                    return b''.join(chunks)
            except requests.RequestException:
                raise ValueError('无法连接 GitHub，请检查网络后重试。') from None
        raise ValueError('更新下载重定向次数过多。')

    def check(self):
        repository = self.store.config.get('repository')
        if not repository:
            raise ValueError('请先在设置中填写公开发布仓库。离线更新可直接导入发布包。')
        if not self.public_key.is_file():
            raise ValueError('尚未配置发布公钥，请先由开发者生成发行包。')
        self.log('正在读取 GitHub Releases…')
        release = json.loads(self._fetch(f'https://api.github.com/repos/{repository}/releases/latest', 2 * 1024 * 1024))
        assets = {row['name']: row for row in release.get('assets', [])}
        index = assets.get('updates.json')
        if not index:
            raise ValueError('该 Release 缺少 updates.json 发布清单。')
        def asset_url(row):
            url = str(row['browser_download_url'])
            prefix = f'https://github.com/{repository}/releases/download/'
            if not url.startswith(prefix):
                raise ValueError('发布附件地址与配置仓库不一致。')
            return url
        manifest = verify(json.loads(self._fetch(asset_url(index), 512 * 1024)), self.public_key)
        if manifest.get('schema') != 1 or not isinstance(manifest.get('packages'), list):
            raise ValueError('发布清单格式不支持。')
        available, downloads = [], {}
        installed = self.store.installed()
        for package in manifest['packages']:
            identifier = package['id']
            if not IDENTIFIER.fullmatch(identifier):
                raise ValueError('发布清单包含无效工具编号。')
            if identifier in installed and version_tuple(package['version']) <= version_tuple(installed[identifier]['current']):
                continue
            asset = assets.get(package['asset'])
            if not asset or not re.fullmatch(r'[a-f0-9]{64}', package['sha256']):
                raise ValueError('发布附件或校验值缺失。')
            if not isinstance(package.get('size'), int) or not 0 < package['size'] <= MAX_ARCHIVE:
                raise ValueError('发布附件大小无效。')
            row = {**package, 'current': installed.get(identifier, {}).get('current', '尚未安装'),
                   'compatible': version_tuple(package['min_hub_version']) <= version_tuple(self.hub_version)}
            available.append(row)
            downloads[identifier] = {**row, 'url': asset_url(asset)}
        with self.lock:
            self.state['available'] = available
            self.release_assets = downloads
        self.log(f'发现 {len(available)} 项新版本。' if available else '当前已是发布的最新版本。')

    def download_install(self, identifier):
        package = self.release_assets.get(identifier)
        if not package:
            raise ValueError('请先检查更新。')
        if not package['compatible']:
            raise ValueError('请先升级工具中心，再更新此工具。')
        self.log('正在下载并校验更新包…')
        content = self._fetch(package['url'], MAX_ARCHIVE, asset=True)
        if len(content) != package['size'] or hashlib.sha256(content).hexdigest() != package['sha256']:
            raise ValueError('下载文件校验失败，当前版本保持不变。')
        self.install(content, expected=(identifier, package['version']))

    def install(self, content, expected=None):
        self.log('正在验证发布签名与文件完整性…')
        manifest, payload = inspect_archive(content, self.public_key)
        identifier, version = manifest['id'], manifest['version']
        installed = self.store.installed()
        if expected and (identifier, version) != expected:
            raise ValueError('下载包与选定版本不一致。')
        if version_tuple(manifest['min_hub_version']) > version_tuple(self.hub_version):
            raise ValueError('请先升级工具中心，再安装这个版本。')
        if identifier in installed and version_tuple(version) <= version_tuple(installed[identifier]['current']):
            raise ValueError('发布包版本不高于当前版本；恢复旧版本请使用“回退”。')
        destination = self.store.version_path(identifier, version)
        if destination.exists():
            raise ValueError('此版本目录已存在，请使用新版本号发布。')
        staging_root = self.store.root / 'updates/staging'
        staging_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='verified-', dir=staging_root))
        moved = False
        try:
            for name, content_file in payload.items():
                target = stage.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content_file)
            self.log('正在检查新版本的运行环境与入口…')
            self.environments.health(identifier, version, stage, self.log)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            moved = True
            # Check again from its final directory before changing the pointer.
            self.environments.health(identifier, version, destination, self.log)
            installed[identifier] = {'current': version, 'previous': installed.get(identifier, {}).get('current')}
            atomic_json(self.store.state / 'installed.json', installed)
        except Exception:
            if moved:
                if destination.resolve().parent != self.store.version_path(identifier, version).parent.resolve():
                    raise ValueError('版本回收路径无效。') from None
                shutil.rmtree(destination)
            raise
        finally:
            if stage.exists():
                if stage.resolve().parent != staging_root.resolve():
                    raise ValueError('暂存路径无效。')
                shutil.rmtree(stage)
        with self.lock:
            self.state['available'] = [row for row in self.state['available'] if row['id'] != identifier]
        try:
            atomic_json(self.store.state / 'last-update.json', {'id': identifier, 'version': version, 'status': 'installed'})
        except OSError:
            self.log('版本已安装，更新记录暂时无法写入。')
        self.log('工具中心新版本已就绪，点击“重启工具中心”后生效。' if identifier == 'hub'
                 else f'更新完成，后续任务将使用 {version}。配置和结果已保留。')

    def rollback(self, identifier):
        installed = self.store.installed()
        record = installed.get(identifier)
        if not record or not record.get('previous'):
            raise ValueError('没有可回退的版本。')
        old = record['previous']
        source = self.store.version_path(identifier, old)
        self.log('正在检查上一版本…')
        self.environments.health(identifier, old, source, self.log)
        installed[identifier] = {'current': old, 'previous': record['current']}
        atomic_json(self.store.state / 'installed.json', installed)
        self.log(f'已回退到 {old}。' + ('重启工具中心后生效。' if identifier == 'hub' else ''))
