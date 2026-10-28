import copy
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import threading
import time
import tempfile
import uuid
from pathlib import Path

from tool_sdk import atomic_json, DEFAULT_BROWSER_PROFILE, ensure_browser_directory

VERSION = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
IDENTIFIER = re.compile(r'^[a-z][a-z0-9-]{0,39}$')


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return copy.deepcopy(fallback)


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError('版本号须为三段数字，例如 1.0.1。')
    return tuple(map(int, value.split('.')))


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(parents=True, exist_ok=True)
        self.outputs = self.root / 'outputs'
        self.outputs.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = self.state / 'tasks.sqlite3'
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)')
            if 'status' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN status TEXT')
                for identifier, payload in db.execute('SELECT id,payload FROM jobs').fetchall():
                    db.execute('UPDATE jobs SET status=? WHERE id=?',(json.loads(payload)['status'],identifier))
            db.execute('CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created)')
        saved = read_json(self.state / 'config.json', {})
        if not isinstance(saved, dict):
            raise ValueError('平台配置格式无效。')
        self.config = {
            'profile': DEFAULT_BROWSER_PROFILE, 'port': 9351,
            'repository': read_json(self.root / 'release-source.json', {}).get('repository', '')}
        self.config.update(saved)
        self.config['profile'] = str(self.config.get('profile') or '').strip() or DEFAULT_BROWSER_PROFILE
        self.browser_directory_error = ''
        try:
            ensure_browser_directory(self.config['profile'])
        except ValueError as exc:
            # Keep the settings page available if this computer has no D: drive or lacks access.
            self.browser_directory_error = str(exc)
        if self.config != saved:
            atomic_json(self.state / 'config.json', self.config)
        if not (self.state / 'installed.json').exists():
            atomic_json(self.state / 'installed.json', {'hub': {'current': '1.0.0', 'previous': None},
                         'boundary': {'current': '1.0.0', 'previous': None},
                         'dmp': {'current': '1.0.0', 'previous': None}})

    @contextmanager
    def connection(self):
        database = sqlite3.connect(self.db, timeout=15)
        try:
            with database:
                yield database
        finally:
            database.close()

    def installed(self):
        return read_json(self.state / 'installed.json', {})

    def version_path(self, identifier, version=None):
        if not IDENTIFIER.fullmatch(identifier):
            raise ValueError('工具编号无效。')
        record = self.installed().get(identifier)
        if not record and version is None:
            raise ValueError('工具尚未安装。')
        value = version or record['current']
        version_tuple(value)
        return self.root / ('app' if identifier == 'hub' else f'tools/{identifier}') / 'versions' / value

    def tool(self, identifier):
        if identifier == 'hub':
            raise ValueError('请从更新管理操作工具中心。')
        manifest = read_json(self.version_path(identifier) / 'tool.json')
        if not manifest or manifest.get('id') != identifier:
            raise ValueError('工具清单无效。')
        return manifest

    def tools(self):
        return [self.tool(key) for key in self.installed() if key != 'hub']

    def save_config(self, payload):
        profile = str(payload.get('profile') or '').strip() or DEFAULT_BROWSER_PROFILE
        if not Path(profile).is_absolute():
            raise ValueError('请填写浏览器用户目录的完整路径。')
        port = int(payload.get('port', 9351))
        if not 1024 <= port <= 65535:
            raise ValueError('浏览器连接端口须为 1024–65535。')
        repository = str(payload.get('repository', '')).strip().removeprefix('https://github.com/').removesuffix('/').removesuffix('.git')
        if repository and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('发布仓库请填写为 账号名/仓库名。')
        ensure_browser_directory(profile)
        with self.lock:
            self.config = {'profile': profile, 'port': port, 'repository': repository}
            atomic_json(self.state / 'config.json', self.config)
            self.browser_directory_error = ''

    def put_job(self, job):
        with self.lock, self.connection() as db:
            db.execute('INSERT OR REPLACE INTO jobs (id,created,payload,status) VALUES (?,?,?,?)',
                       (job['id'], job['created'], json.dumps(job, ensure_ascii=False),job['status']))

    def job(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('没有找到这项任务。')
        return json.loads(row[0])

    def jobs(self, limit=200):
        with self.connection() as db:
            rows = db.execute('SELECT payload FROM jobs ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        result = [json.loads(row[0]) for row in rows]
        seen = {job['id'] for job in result}
        result.extend(job for job in self.unfinished() if job['id'] not in seen)
        return sorted(result,key=lambda job:job['created'],reverse=True)

    def unfinished(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs WHERE status IN ('queued','running','stopping')")]

    def catalog(self, identifier):
        data = self.state / 'tools' / identifier
        if identifier == 'boundary':
            return read_json(data / 'data/categories.json', {'roots': [], 'children': {}})
        if identifier == 'dmp':
            return read_json(data / 'category_catalog.json', {'parents': []})
        raise ValueError('工具没有类目库。')

    def results(self):
        rows = []
        for path in self.outputs.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in {'.xlsx', '.json'} or path.name.startswith('.'):
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(self.outputs) or path.name.endswith('.tmp.json'):
                continue
            rel = path.relative_to(self.outputs)
            rows.append({'path': rel.as_posix(), 'name': path.name, 'bytes': path.stat().st_size,
                         'modified': path.stat().st_mtime,
                         'tool_id': rel.parts[0] if rel.parts[0] in self.installed() else
                             next((key for key in self.installed() if key != 'hub' and path.name.startswith(key + '_')), 'boundary')})
        return sorted(rows, key=lambda r: r['modified'], reverse=True)[:300]

    def save_export(self, tool_id, name, content):
        self.tool(tool_id)
        name = Path(name).name
        suffix = Path(name).suffix.lower()
        if suffix not in {'.xlsx', '.json'}:
            raise ValueError('只支持归档 Excel 和 JSON 结果。')
        name = Path(name).stem[:100] + suffix
        descriptor, temporary = tempfile.mkstemp(prefix='.export-', suffix='.tmp', dir=self.outputs)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
            with self.lock:
                while True:
                    target = self.outputs / (tool_id + time.strftime('_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:10] + '_' + name)
                    if target.exists():
                        continue
                    try:
                        temporary.rename(target)
                        return target.name
                    except FileExistsError:
                        continue
        finally:
            temporary.unlink(missing_ok=True)

    def publish_workspace(self, tool_id, workspace):
        workspace = Path(workspace).resolve()
        published = {}
        if not workspace.is_dir():
            return published
        for path in sorted(workspace.iterdir()):
            if (path.is_file() and not path.is_symlink() and path.suffix.lower() in {'.xlsx', '.json'}
                    and not path.name.startswith('.') and not path.name.endswith('.tmp.json')):
                published[path.name] = self.save_export(tool_id, path.name, path.read_bytes())
        return published
