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
APP=ROOT/'app/versions/1.0.5'
SOURCE=ROOT/'tools/dmp/versions/1.0.2'
sys.path.insert(0,str(APP))
from hub import Application,Handler,ThreadingHTTPServer
from tool_sdk import atomic_json


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        atomic_json(self.root/'state/installed.json',{'hub':{'current':'1.0.1'},'boundary':{'current':'1.0.0'},'dmp':{'current':'1.0.2'}})
        target=self.root/'tools/dmp/versions/1.0.2'
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
