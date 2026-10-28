import hashlib
from contextlib import closing
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app/versions/1.0.5'))
from hub import Application, Handler, ThreadingHTTPServer
from tool_sdk import atomic_json
from openpyxl import load_workbook


class PDDIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='pdd-hub-test-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'tools/pdd-review/versions/1.0.2'
        shutil.copytree(ROOT / 'tools/pdd-review/versions/1.0.2', cls.source,
                        ignore=shutil.ignore_patterns('__pycache__'))
        atomic_json(cls.root / 'state/installed.json', {
            'hub': {'current': '1.0.5', 'previous': None},
            'pdd-review': {'current': '1.0.2', 'previous': None}})
        cls.app = Application(cls.root)
        cls.app.pdd_portal.auto_browser = False
        # Reuse only verified code/dependencies; SQLite and browser state remain isolated.
        prepared = ROOT / 'runtime/node-envs/pdd-review/1.0.1'
        if (prepared / 'ready.json').exists():
            for folder in ('src', 'public', 'test'):
                for file in (cls.source / folder).rglob('*'):
                    if file.is_file():
                        assert file.read_bytes() == (prepared / file.relative_to(cls.source)).read_bytes()
            cls.app.pdd_portal._prepare = lambda: (shutil.which('node'), prepared)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.app = cls.app
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.app.pdd_portal.stop(force=True)
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.temp.cleanup()

    def request(self, route, method='GET', body=None, authorized=True, origin=True):
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['X-Local-Token'] = self.app.token
        if origin:
            headers['Origin'] = self.base
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + route, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=240) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            with response:
                return response.code, response.read()

    def test_original_layout_and_business_modules_preserved(self):
        hashes = json.loads((self.source / 'original-source.json').read_text())['files']
        for name in ('src/collector.js', 'src/database.js', 'src/formatters.js', 'public/styles.css', 'test/collector.test.js'):
            self.assertEqual(hashlib.sha256((self.source / name).read_bytes()).hexdigest(), hashes[name])
        self.assertFalse(list(self.source.rglob('.env*')))
        self.assertFalse((self.source / 'dingtalk-login').exists())
        server = (self.source / 'src/server.js').read_text(encoding='utf-8')
        self.assertNotIn('DingTalk', server)
        self.assertNotIn('DINGTALK', server)
        html = self.app.pdd_portal.html().decode()
        for control in ('openBrowserButton', 'startButton', 'maxReviews', 'delayMs', 'latestFirst',
                        'currentJob', 'exportButton', 'clearButton', 'selectAllProducts'):
            self.assertIn(f'id="{control}"', html)
        js = self.app.pdd_portal.javascript().decode()
        self.assertIn('X-Local-Token', js)
        self.assertNotIn(self.app.token, js)  # A cross-site script include must not disclose a token.
        self.assertNotIn('DELETE', js[js.index('async function initialize()'):])

    def test_proxy_data_export_and_lifecycle(self):
        status, body = self.request('/tools/pdd-review/api/status')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['browserConnected'])
        with closing(sqlite3.connect(self.root / 'state/tools/pdd-review/comments.db')) as db, db:
            db.execute('INSERT INTO products VALUES (?,?,?,?)', ('123456789012', '2026-10-27', '2026-10-27', 1))
            db.execute('INSERT INTO reviews (review_id,goods_id,comment,username,specs,raw_json,collected_at) VALUES (?,?,?,?,?,?,?)',
                ('test-only', '123456789012', '本地测试评论', '测试用户', '[{"spec_key":"型号","spec_value":"测试型号"}]', '{}', '2026-10-27'))
        for _ in range(2):
            self.assertEqual(self.request('/tools/pdd-review/')[0], 200)
            self.assertEqual(len(json.loads(self.request('/tools/pdd-review/api/status')[1])['products']), 1)
        path = '/tools/pdd-review/api/export.xlsx?goods_id=123456789012&_token=' + self.app.token
        status, excel = self.request(path, authorized=False)
        self.assertEqual(status, 200)
        sheet = load_workbook(io.BytesIO(excel)).active
        self.assertEqual(list(sheet.values), [('用户名称', '商品型号', '评论'), ('测试用户', '测试型号', '本地测试评论')])
        exports = list((self.root / 'outputs').glob('pdd-review_*.xlsx'))
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].read_bytes(), excel)
        with patch.object(self.app.pdd_portal, 'info', return_value={'busy': True}):
            self.assertEqual(self.request('/api/pdd/stop', 'POST', {})[0], 400)
            with self.assertRaises(ValueError):
                self.app.tasks.reserve_update()
            self.assertFalse(self.app.tasks.maintenance)
        self.assertEqual(self.request('/tools/pdd-review/api/products', 'DELETE', {'all': True})[0], 200)
        self.assertEqual(json.loads(self.request('/tools/pdd-review/api/status')[1])['products'], [])
        self.assertEqual(self.request('/api/pdd/stop', 'POST', {})[0], 200)
        # A pending status poll cannot resurrect a service after explicit stop.
        self.assertEqual(self.request('/tools/pdd-review/api/status')[0], 400)
        self.assertEqual(self.request('/tools/pdd-review/')[0], 200)
        self.assertEqual(self.request('/tools/pdd-review/api/status')[0], 200)

    def test_security_boundaries(self):
        for path in ('/tools/pdd-review/', '/tools/pdd-review/api/status', '/tools/pdd-review/api/export.xlsx'):
            self.assertEqual(self.request(path, authorized=False)[0], 403)
        self.assertEqual(self.request('/tools/pdd-review/api/products', 'DELETE', {'all': True}, origin=False)[0], 403)
        self.assertEqual(self.request('/tools/pdd-review/api/unknown')[0], 400)
        self.request('/tools/pdd-review/')
        self.request('/tools/pdd-review/api/status')
        import requests
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f'http://127.0.0.1:{self.app.pdd_portal.port}/api/status', timeout=3)
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.app.pdd_portal.info()['browser_connected'])

    def test_rollback_cannot_remove_required_host_support(self):
        installed = self.app.store.installed()
        changed = json.loads(json.dumps(installed))
        changed['hub']['previous'] = '1.0.1'
        atomic_json(self.root / 'state/installed.json', changed)
        try:
            with self.assertRaisesRegex(ValueError, '拼多多.*1.0.5'):
                self.app.updates.rollback('hub')
        finally:
            atomic_json(self.root / 'state/installed.json', installed)

    def test_shared_browser_node_lifecycle(self):
        import subprocess
        node, home = self.app.pdd_portal._prepare()
        result = subprocess.run([node, '--test', 'test/shared-browser.test.js'], cwd=home,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace'))

    def test_shared_capture_waits_for_other_tools(self):
        with patch.object(self.app.store, 'unfinished', return_value=[{'id': 'other-tool-job'}]):
            for path in ('/api/browser/open', '/api/capture-current'):
                code, body = self.request('/tools/pdd-review' + path, 'POST', {})
                self.assertEqual(code, 400)
                self.assertIn('队列', json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
