import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parent
APP=ROOT/'app/versions/1.0.6'
SOURCE=ROOT/'tools/dmp/versions/1.0.3'
sys.path.insert(0,str(APP))
from hub import Application,Handler,ThreadingHTTPServer
from tool_sdk import atomic_json


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        atomic_json(self.root/'state/installed.json',{'hub':{'current':'1.0.1'},'boundary':{'current':'1.0.0'},'dmp':{'current':'1.0.3'}})
        target=self.root/'tools/dmp/versions/1.0.3'
        shutil.copytree(SOURCE,target,ignore=shutil.ignore_patterns('__pycache__','defaults'))
        boundary=self.root/'tools/boundary/versions/1.0.0'
        boundary.mkdir(parents=True)
        atomic_json(boundary/'tool.json',{'id':'boundary','name':'boundary','version':'1.0.0','actions':['collect']})
        (boundary/'requirements.txt').write_text('',encoding='utf-8')
        (boundary/'adapter.py').write_text("from tool_sdk import run\ndef main(ctx):ctx.stop.wait(2);return {}\nif __name__=='__main__':run(main,lambda:None)\n",encoding='utf-8')
        catalog={'version':2,'complete':True,'parents':[{'category_id':'50002768','category_name':'个人护理','children_loaded':True,'children':[{'category_id':'350213','category_name':'电吹风','children_loaded':True,'children':[]}]}]}
        atomic_json(self.root/'state/tools/dmp/category_catalog.json',catalog)
        self.app=Application(self.root)
        self.app.environments.ensure=Mock(return_value=Path(sys.executable))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True;self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.params={'query_mode':'second','selected_ids':['350213'],'start_date':'2026-08-01','end_date':'2026-09-30',
                     'monthly':True,'user_data_dir':str(self.root/'profile'),'port':9351,'interval':1,
                     'filename':'我的覆盖人数.xlsx','price_ranges':[{'min_amount':'0','max_amount':'100'}]}

    def tearDown(self):
        for job in self.app.store.unfinished():self.app.tasks.stop(job['id'])
        end=time.monotonic()+5
        while self.app.store.unfinished() and time.monotonic()<end:time.sleep(.05)
        self.app.dmp_portal.close()
        self.server.shutdown();self.server.server_close();self.temp.cleanup()

    def request(self,path,payload=None,headers=None):
        values={'X-Local-Token':self.app.token,'Origin':self.base}
        values.update(headers or {})
        request=urllib.request.Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=values)
        try:
            with urllib.request.urlopen(request,timeout=15) as response:return response.status,response.read(),dict(response.headers)
        except urllib.error.HTTPError as exc:
            try:return exc.code,exc.read(),dict(exc.headers)
            finally:exc.close()

    def test_retained_page_controls_theme_and_scoped_transport(self):
        status,content,headers=self.request('/tools/dmp/')
        self.assertEqual(status,200)
        html=content.decode()
        original=(SOURCE/'web/index.html').read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'\bid="([^"]+)"',html),re.findall(r'\bid="([^"]+)"',original))
        for text in ['一级类目汇总','二级类目明细','三级类目明细','已选类目与金额区间','常用类目组合','常用金额配置','预览任务清单','确认并开始','继续未完成采集','导出文件名']:
            self.assertIn(text,html)
        self.assertNotIn("'/api/",html)
        self.assertIn("headers:data===undefined?{'X-Local-Token':TOKEN}",html)
        self.assertEqual(headers['X-Frame-Options'],'SAMEORIGIN')
        theme=(SOURCE/'web/portal-theme.css').read_text()
        self.assertNotRegex(theme,r'grid-template|(?:^|[;{])(?:width|height|display|order)\s*:')

    def test_original_state_preview_templates_and_settings_through_proxy(self):
        profile=str(self.root/'customer-browser')
        self.app.store.config['profile']=profile
        status,body,_=self.request('/tools/dmp/api/state')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['parents'][0]['category_id'],'50002768')
        self.assertEqual(json.loads(body)['settings']['user_data_dir'],profile)
        status,body,_=self.request('/tools/dmp/api/preview',self.params)
        self.assertEqual(status,200,body)
        value=json.loads(body)
        self.assertEqual(value['count'],2)
        self.assertEqual(value['settings']['filename'],'我的覆盖人数.xlsx')
        status,body,_=self.request('/tools/dmp/api/price-preset',{'action':'save','name':'常用档位','price_ranges':[{'min_amount':'10','max_amount':'20','highlight':True}]})
        self.assertEqual(status,200,body)
        self.assertEqual(self.request('/tools/dmp/api/settings',self.params)[0],200)
        state=json.loads(self.request('/tools/dmp/api/state')[1])
        self.assertIn('常用档位',state['price_presets'])
        self.assertEqual(state['settings']['filename'],'我的覆盖人数.xlsx')

    def test_prefixed_routes_reject_cross_origin_missing_token_and_escaping_download(self):
        self.assertEqual(self.request('/tools/dmp/api/state',headers={'X-Local-Token':''})[0],403)
        self.assertEqual(self.request('/tools/dmp/api/settings',self.params,{'Origin':'https://evil.test'})[0],403)
        url='/tools/dmp/api/download?_token='+self.app.token+'&file=../../../state/installed.json'
        self.assertEqual(self.request(url)[0],400)
        self.assertEqual(self.request('/tools/dmp/api/page/close',{'_token':self.app.token}, {'X-Local-Token':''})[0],200)

    def test_original_start_joins_shared_queue_and_cancel_prevents_collection(self):
        self.assertEqual(self.request('/tools/dmp/api/state')[0],200)
        first=self.app.tasks.submit('boundary','collect',{})
        self.assertEqual(self.request('/tools/dmp/api/start',self.params)[0],200)
        jobs=self.app.store.jobs()
        queued=next(job for job in jobs if job['tool_id']=='dmp')
        self.assertEqual(queued['status'],'queued')
        self.assertTrue(queued['params']['_portal_ticket'])
        self.assertEqual(self.request('/tools/dmp/api/stop',{})[0],200)
        self.assertEqual(self.app.store.job(queued['id'])['status'],'cancelled')
        state=json.loads(self.request('/tools/dmp/api/state')[1])
        self.assertFalse(state['task']['running'])

    def test_platform_browser_overrides_page_and_updates_without_sidecar_restart(self):
        self.assertEqual(self.request('/tools/dmp/api/state')[0],200)
        process=self.app.dmp_portal.process.pid
        payload={**self.params,'user_data_dir':str(self.root/'wrong-page-profile'),'port':9999}
        self.assertEqual(self.request('/tools/dmp/api/settings',payload)[0],200)
        state=json.loads(self.request('/tools/dmp/api/state')[1])
        self.assertEqual(state['settings']['user_data_dir'],self.app.store.config['profile'])
        self.assertEqual(state['settings']['port'],self.app.store.config['port'])
        selected=str(self.root/'new-platform-profile')
        self.assertEqual(self.request('/api/settings',{'profile':selected,'port':9352,'repository':''})[0],200)
        preview=json.loads(self.request('/tools/dmp/api/preview',payload)[1])
        self.assertEqual(preview['settings']['user_data_dir'],selected)
        self.assertEqual(preview['settings']['port'],9352)
        self.assertEqual(self.app.dmp_portal.process.pid,process)
        self.app.tasks.submit('boundary','collect',{})
        self.assertEqual(self.request('/tools/dmp/api/start',payload)[0],200)
        queued=next(j for j in self.app.store.jobs() if j['tool_id']=='dmp')
        self.assertEqual(queued['params']['_portal_browser'],{'profile':selected,'port':9352})
        self.assertEqual(queued['browser'],{'profile':selected,'port':9352})
        self.request('/tools/dmp/api/stop',{})

    def test_resume_uses_platform_snapshot_instead_of_old_checkpoint_browser(self):
        import subprocess
        program='''import json,os
from pathlib import Path
from unittest.mock import Mock,patch
import portal_service as service
data=Path(os.environ['TOOL_DATA_DIR'])
selected={'profile':str(data/'platform-profile'),'port':9352}
app=service.ManagedApplication(data,data/'resume-workspace',Mock(),lambda:dict(selected))
params=json.loads(os.environ['TEST_DMP_PARAMS'])
params.update(user_data_dir=str(data/'legacy-wrong-profile'),port=9999)
settings=service.original.normalize_settings(params,app.catalog,app.price_presets,app.category_template_assignments)
service.original.save_progress(app.outputs/service.original.PROGRESS_NAME,settings,[])
app.resume(service.original.PROGRESS_NAME)
ticket=app.active_ticket
accepted=dict(app.entries[ticket]['browser'])
assert accepted==selected
selected.update(profile=str(data/'later-profile'),port=9353)
with patch.object(service.Context,'verify_browser'),patch.object(service.original.LocalApplication,'collect') as collect:
 app.execute(ticket);app.worker.join(5)
 assert not app.worker.is_alive()
 actual=collect.call_args.args[0]
 assert actual['user_data_dir']==accepted['profile'] and actual['port']==accepted['port']
assert app.entries[ticket]['phase']=='finished'
'''
        env=dict(os.environ)
        env.update(PYTHONPATH=os.pathsep.join((str(APP),str(SOURCE))),
                   TOOL_DATA_DIR=str(self.root/'state/tools/dmp'),TEST_DMP_PARAMS=json.dumps(self.params))
        result=subprocess.run([sys.executable,'-X','utf8','-c',program],cwd=SOURCE,env=env,
                              capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))

    def test_stale_browser_submission_is_rejected_before_queuing(self):
        self.assertEqual(self.request('/tools/dmp/api/state')[0],200)
        status,body,_=self.request('/api/internal/dmp-submit',{'ticket':'a'*32,'kind':'coverage',
            'browser':{'profile':str(self.root/'stale-profile'),'port':9999}})
        self.assertEqual(status,400,body)
        self.assertFalse(self.app.store.unfinished())


class OriginalSourceTests(unittest.TestCase):
    def test_original_source_files_are_retained_byte_for_byte(self):
        original=Path('D:/cmProject/dmp/dmp_zdyqrysc')
        pairs=[('webapp.py','original_webapp.py'),('web/index.html','web/index.html'),
               *[(name,name) for name in ('coverage.py','batch.py','category_catalog.py','category_reader.py')]]
        expected=json.loads((ROOT/'docs/dmp-original-source-sha256.json').read_text(encoding='utf-8'))
        for before,after in pairs:
            with self.subTest(file=before):
                actual=hashlib.sha256((SOURCE/after).read_bytes()).hexdigest()
                self.assertEqual(actual,expected[after])
                if (original/before).is_file():
                    self.assertEqual(hashlib.sha256((original/before).read_bytes()).hexdigest(),actual)


if __name__=='__main__':unittest.main()
