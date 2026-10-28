import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import CollectionError
from webapp import LocalApplication,LocalServer,PageSessions


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        catalog={'roots':[{'id':'1','name':'一级'}], 'children':{'1':[{'id':'2','name':'二级'}], '2':[{'id':'3','name':'三级'}]}}
        (self.folder/'data').mkdir()
        (self.folder/'data/categories.json').write_text(json.dumps(catalog),encoding='utf-8')
        self.app=LocalApplication(self.folder)
        self.server=LocalServer(('127.0.0.1',0),self.app)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.stop.set()
        if self.app.worker:
            self.app.worker.join(2)
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.temp.cleanup()

    def request(self,method,route,payload=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={'Content-Type':'application/json','X-Local-Token':self.server.token,'Origin':f'http://127.0.0.1:{self.port}'}
        h.update(headers or {})
        c.request(method,route,json.dumps(payload) if payload is not None else None,h)
        r=c.getresponse();status,body=r.status,r.read();c.close()
        return status,body

    def payload(self):
        return {'root_id':'1','second_id':'2','third_id':'3','period':'month','date':'2026-09',
                'profile':str(self.folder/'profile'),'port':9352}

    def test_html_and_authenticated_state(self):
        status,raw=self.request('GET','/')
        self.assertEqual(status,200)
        self.assertIn('词根分析采集',raw.decode())
        self.assertNotIn('__LOCAL_TOKEN__',raw.decode())
        status,raw=self.request('GET','/api/state')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['catalog']['roots'][0]['id'],'1')
        self.assertEqual(self.request('GET','/api/state',headers={'X-Local-Token':'bad'})[0],403)

    def test_cross_origin_and_rebinding_rejected(self):
        self.assertEqual(self.request('POST','/api/stop',{}, {'Origin':'https://other.example'})[0],403)
        self.assertEqual(self.request('GET','/',headers={'Host':'attacker.example'})[0],403)

    def test_save_settings_and_category_lineage(self):
        self.assertEqual(self.request('POST','/api/settings',self.payload())[0],200)
        saved=json.loads(self.app.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['third_id'],'3')
        invalid=self.payload();invalid['second_id']='3'
        self.assertEqual(self.request('POST','/api/settings',invalid)[0],400)
        self.assertEqual(json.loads(self.app.settings_path.read_text())['second_id'],'2')

    def test_concurrent_operation_rejected_and_stop_allowed(self):
        self.app.begin('test',lambda:self.app.stop.wait(3))
        self.assertEqual(self.request('POST','/api/start',self.payload())[0],400)
        self.assertEqual(self.request('POST','/api/stop',{})[0],200)
        self.app.worker.join(2)
        self.assertFalse(self.app.task['running'])

    def test_download_only_result_files(self):
        (self.app.outputs/'sample.xlsx').write_bytes(b'xlsx')
        (self.folder/'secret.xlsx').write_bytes(b'secret')
        status,raw=self.request('GET','/api/download?file=sample.xlsx')
        self.assertEqual((status,raw),(200,b'xlsx'))
        for name in ['../secret.xlsx','..%2Fsecret.xlsx','sample.json','missing.xlsx']:
            self.assertEqual(self.request('GET','/api/download?file='+name)[0],404)

    def test_failed_job_reports_error_and_finishes(self):
        def failed():
            raise CollectionError('登录失效')
        self.app.begin('test',failed)
        self.app.worker.join(2)
        state=self.app.snapshot()
        self.assertFalse(state['task']['running'])
        self.assertEqual(state['task']['error'],'登录失效')
        self.assertIsNone(state['task']['output'])

    def test_stop_page_requires_token(self):
        self.assertEqual(self.request('POST','/api/page/close',{'page_id':'example-page','_token':'bad'})[0],403)


class LifecycleTests(unittest.TestCase):
    def test_refresh_grace_multiple_pages_and_shutdown(self):
        sessions=PageSessions()
        sessions.update('first-page')
        sessions.update('other-page')
        sessions.update('first-page',closed=True)
        self.assertFalse(sessions.should_stop())
        sessions.update('other-page',closed=True)
        self.assertFalse(sessions.should_stop(sessions.closed_at+9))
        sessions.update('new-refresh')
        self.assertFalse(sessions.should_stop())
        sessions.update('new-refresh',closed=True)
        self.assertTrue(sessions.should_stop(sessions.closed_at+11))


if __name__=='__main__':
    unittest.main()
