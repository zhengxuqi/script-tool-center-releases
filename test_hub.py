import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent
APP = ROOT / ('app/versions/' + os.environ.get('HUB_TEST_VERSION', '1.0.0'))
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / 'publisher'))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tool_sdk import atomic_json
from hubcore.storage import Store
from hubcore.runtime import TaskQueue
from hubcore.updates import Updates, inspect_archive
from release import build_package, sign
from hub import Application, Handler, ThreadingHTTPServer


def eventually(predicate, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError('Condition did not become true')


def seed(root):
    store = Store(root)
    for identifier in ('boundary','dmp'):
        source = store.version_path(identifier)
        source.mkdir(parents=True)
        atomic_json(source / 'tool.json', {'id':identifier,'name':identifier,'version':'1.0.0',
            'min_hub_version':'1.0.0','actions':['collect','preview'],'supports_resume':identifier=='dmp'})
        (source / 'requirements.txt').write_text('', encoding='utf-8')
        (source / 'adapter.py').write_text('''from tool_sdk import run,atomic_json
def main(ctx):
    ctx.log('started')
    for _ in range(ctx.params.get('steps',10)):
        if ctx.stop.wait(.03):
            raise ValueError('stopped')
    atomic_json(ctx.output/'result.json', {'value':42})
    return {'ok':True}
if __name__ == '__main__': run(main, lambda:None)
''', encoding='utf-8')
    return store


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = seed(self.root)
        self.private = Ed25519PrivateKey.generate()
        self.public = self.root / 'trust/release-public-key.pem'
        self.public.parent.mkdir()
        self.public.write_bytes(self.private.public_key().public_bytes(serialization.Encoding.PEM,
                                serialization.PublicFormat.SubjectPublicKeyInfo))
        self.env = Mock()
        self.tasks = Mock()
        self.updates = Updates(self.store,self.env,self.tasks,'1.0.0')

    def tearDown(self):
        self.temp.cleanup()

    def package(self, version='1.0.1'):
        metadata = build_package(self.store.version_path('boundary'),'boundary',version,self.private,self.root/'dist')
        return (self.root/'dist'/metadata['asset']).read_bytes()

    def rewrite(self, content, change):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {name:archive.read(name) for name in archive.namelist()}
        change(files)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            for name,value in files.items(): archive.writestr(name,value)
        return stream.getvalue()

    def test_signed_install_keeps_user_data_and_atomic_rollback(self):
        settings = self.store.state/'tools/boundary/settings.json'
        atomic_json(settings,{'profile':'do-not-replace'})
        self.updates.install(self.package())
        self.assertEqual(self.store.installed()['boundary'],{'current':'1.0.1','previous':'1.0.0'})
        self.assertEqual(json.loads(settings.read_text()),{'profile':'do-not-replace'})
        self.updates.rollback('boundary')
        self.assertEqual(self.store.installed()['boundary']['current'],'1.0.0')
        self.assertTrue(self.store.version_path('boundary','1.0.1').is_dir())

    def test_failed_health_preserves_current_version(self):
        self.env.health.side_effect=ValueError('bad new version')
        with self.assertRaises(ValueError): self.updates.install(self.package())
        self.assertEqual(self.store.installed()['boundary']['current'],'1.0.0')
        self.assertFalse(self.store.version_path('boundary','1.0.1').exists())

    def test_install_new_tool_and_failed_new_tool_leave_registry_correct(self):
        package=build_package(self.store.version_path('boundary'),'extra-tool','1.0.0',self.private,self.root/'dist')
        content=(self.root/'dist'/package['asset']).read_bytes()
        self.updates.install(content)
        self.assertEqual(self.store.tool('extra-tool')['version'],'1.0.0')
        self.assertIsNone(self.store.installed()['extra-tool']['previous'])
        package=build_package(self.store.version_path('boundary'),'broken-tool','1.0.0',self.private,self.root/'dist')
        self.env.health.side_effect=[None,ValueError('bad final path')]
        with self.assertRaisesRegex(ValueError,'bad final path'):
            self.updates.install((self.root/'dist'/package['asset']).read_bytes())
        self.assertNotIn('broken-tool',self.store.installed())
        self.assertFalse(self.store.version_path('broken-tool','1.0.0').exists())

    def test_failure_after_move_rolls_back_files_and_pointer(self):
        self.env.health.side_effect=[None,ValueError('bad final path')]
        with self.assertRaises(ValueError): self.updates.install(self.package())
        self.assertEqual(self.store.installed()['boundary']['current'],'1.0.0')
        self.assertFalse(self.store.version_path('boundary','1.0.1').exists())

    def test_tampered_payload_and_signature_are_rejected(self):
        content=self.package()
        altered=self.rewrite(content,lambda files:files.update({'payload/adapter.py':b'evil'}))
        with self.assertRaisesRegex(ValueError,'校验'): inspect_archive(altered,self.public)
        def signature(files):
            value=json.loads(files['bundle.json']);value['signature']=base64.b64encode(b'x'*64).decode();files['bundle.json']=json.dumps(value).encode()
        altered=self.rewrite(content,signature)
        with self.assertRaisesRegex(ValueError,'签名'): inspect_archive(altered,self.public)

    def test_zip_traversal_unlisted_duplicate_and_windows_paths_rejected(self):
        content=self.package()
        for name in ('../state/config.json','payload/C:/oops','payload/a\\b.py','payload/CON.py','payload/extra.py','payload/ADAPTER.py'):
            with self.subTest(name=name):
                altered=self.rewrite(content,lambda files:files.update({name:b'bad'}))
                with self.assertRaises(ValueError): inspect_archive(altered,self.public)

    def test_incompatible_update_rejected_without_switch(self):
        content=self.package()
        def incompatible(files):
            import hashlib
            envelope=json.loads(files['bundle.json'])
            envelope['manifest']['min_hub_version']='9.0.0'
            tool=json.loads(files['payload/tool.json'])
            tool['min_hub_version']='9.0.0'
            files['payload/tool.json']=json.dumps(tool).encode()
            envelope['manifest']['files']['tool.json']=hashlib.sha256(files['payload/tool.json']).hexdigest()
            files['bundle.json']=json.dumps(sign(envelope['manifest'],self.private)).encode()
        with self.assertRaisesRegex(ValueError,'先升级'): self.updates.install(self.rewrite(content,incompatible))
        self.env.health.assert_not_called()

    def test_github_check_matches_signed_index_and_asset(self):
        package=build_package(self.store.version_path('boundary'),'boundary','1.0.1',self.private,self.root/'dist')
        self.store.config['repository']='owner/releases'
        index=json.dumps(sign({'schema':1,'packages':[package]},self.private)).encode()
        assets=[{'name':'updates.json','browser_download_url':'https://github.com/owner/releases/releases/download/v1/updates.json'},
                {'name':package['asset'],'browser_download_url':'https://github.com/owner/releases/releases/download/v1/'+package['asset']}]
        self.updates._fetch=Mock(side_effect=[json.dumps({'assets':assets}).encode(),index])
        self.updates.check()
        self.assertEqual(self.updates.snapshot()['available'][0]['version'],'1.0.1')
        self.updates._fetch=Mock(return_value=(self.root/'dist'/package['asset']).read_bytes())
        self.updates.download_install('boundary')
        self.assertEqual(self.store.installed()['boundary']['current'],'1.0.1')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=seed(self.temp.name)
        self.env=Mock();self.env.ensure.return_value=Path(sys.executable)
        self.queue=TaskQueue(self.store,self.env)

    def tearDown(self):
        eventually(lambda:not self.store.unfinished())
        self.temp.cleanup()

    def test_serial_queue_and_cancel_waiting_job(self):
        first=self.queue.submit('boundary','collect',{'steps':25})
        eventually(lambda:self.store.job(first['id'])['status']=='running')
        second=self.queue.submit('dmp','collect',{})
        self.assertEqual(self.store.job(second['id'])['status'],'queued')
        self.queue.stop(second['id'])
        eventually(lambda:self.store.job(first['id'])['status']=='completed')
        self.assertEqual(self.store.job(second['id'])['status'],'cancelled')
        self.assertFalse((self.store.state/'jobs'/second['id']/'request.json').exists())

    def test_running_stop_preserves_task_history(self):
        job=self.queue.submit('boundary','collect',{'steps':150})
        eventually(lambda:self.store.job(job['id']).get('pid'))
        self.queue.stop(job['id'])
        eventually(lambda:self.store.job(job['id'])['status']=='cancelled')
        self.assertTrue(self.store.job(job['id'])['logs'])

    def test_update_blocks_submission_and_pending_jobs_block_update(self):
        self.queue.reserve_update()
        with self.assertRaises(ValueError):self.queue.submit('boundary','collect',{})
        self.queue.release_update()
        job=self.queue.submit('boundary','collect',{})
        with self.assertRaises(ValueError):self.queue.reserve_update()
        eventually(lambda:not self.store.unfinished())

    def test_restart_marks_previous_unfinished_tasks_interrupted(self):
        job={'id':'old-task','tool_id':'boundary','action':'collect','created':1,'status':'queued','params':{},'logs':[]}
        self.store.put_job(job)
        other=TaskQueue(self.store,self.env)
        self.assertEqual(self.store.job(job['id'])['status'],'interrupted')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();seed(self.temp.name)
        self.app=Application(self.temp.name)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True;self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        values={'X-Local-Token':self.app.token,'Origin':self.base}
        if headers:values.update(headers)
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=values)
        try:
            with urllib.request.urlopen(request) as response:return response.status,response.read()
        except urllib.error.HTTPError as exc:
            try:return exc.code,exc.read()
            finally:exc.close()

    def test_state_static_and_csrf(self):
        self.assertEqual(self.request('/')[0],200)
        self.assertEqual(self.request('/api/state')[0],200)
        self.assertEqual(self.request('/api/state',headers={'X-Local-Token':''})[0],403)
        self.assertEqual(self.request('/api/settings',{},headers={'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('/api/health',headers={'Host':'evil.test'})[0],403)

    def test_download_stays_inside_results(self):
        (self.app.store.outputs/'ok.xlsx').write_bytes(b'xlsx')
        (self.app.store.root/'secret.json').write_text('private')
        self.assertEqual(self.request('/api/download?path=ok.xlsx'),(200,b'xlsx'))
        self.assertEqual(self.request('/api/download?path=../secret.json')[0],400)
        self.assertEqual(self.request('/api/download?path=../state/config.json')[0],400)


if __name__=='__main__':unittest.main()
