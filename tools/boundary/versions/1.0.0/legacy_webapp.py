"""Loopback-only web interface for the BI collector."""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,quote,urlsplit

from collector import BIClient,CollectionError,DEFAULT_PORT,DEFAULT_PROFILE,ROOT,Selection,atomic_json,validate_categories,group_category_tree


def load_json(path,fallback):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return fallback


class LocalApplication:
    def __init__(self,root=ROOT,client_factory=BIClient):
        self.root=Path(root)
        self.outputs=self.root/'outputs'
        self.outputs.mkdir(parents=True,exist_ok=True)
        self.client_factory=client_factory
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.worker=None
        self.client=None
        self.closing=False
        self.catalog_path=self.root/'data/categories.json'
        self.settings_path=self.root/'settings.json'
        self.catalog=load_json(self.catalog_path,{'roots':[],'children':{}})
        self.settings={'profile':str(DEFAULT_PROFILE),'port':DEFAULT_PORT,'root_id':'50002768',
                       'second_id':'','third_id':'','period':'7','date':''}
        saved=load_json(self.settings_path,{})
        if isinstance(saved,dict):
            self.settings.update({k:v for k,v in saved.items() if k in self.settings})
        self.limits=None
        self.limits_category=None
        self.task={'running':False,'kind':None,'status':'就绪','logs':[], 'error':None,'output':None,'counts':None}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({'settings':self.settings,'catalog':self.catalog,'task':self.task,
                                  'limits':self.limits,'limits_category':self.limits_category,
                                  'history':self.history(),'closing':self.closing})

    def history(self):
        rows=[]
        for path in self.outputs.glob('*.xlsx'):
            if path.name.endswith('.tmp.xlsx'):
                continue
            stat=path.stat()
            rows.append({'name':path.name,'bytes':stat.st_size,'modified':stat.st_mtime})
        return sorted(rows,key=lambda r:r['modified'],reverse=True)[:30]

    def log(self,message):
        with self.lock:
            self.task['logs']=(self.task['logs']+[str(message)])[-100:]
            self.task['status']=str(message)

    def config(self,payload):
        profile=str(payload.get('profile',self.settings['profile'])).strip()
        if not profile:
            raise CollectionError('浏览器资料目录不能为空。')
        try:
            port=int(payload.get('port',self.settings['port']))
        except (ValueError,TypeError):
            raise CollectionError('连接端口必须为数字。')
        if not 1024<=port<=65535:
            raise CollectionError('连接端口须在1024–65535之间。')
        return {'profile':profile,'port':port}

    def selection(self,payload):
        roots=validate_categories(self.catalog.get('roots',[]))
        first=str(payload.get('root_id',''))
        root=next((r for r in roots if r['id']==first),None)
        if not root:
            raise CollectionError('请选择当前类目列表中的一级类目。')
        selected=[root]
        second=str(payload.get('second_id',''))
        third=str(payload.get('third_id',''))
        if third and not second:
            raise CollectionError('请选择三级类目所属的二级类目。')
        for parent,identifier in [(first,second),(second,third)]:
            if not identifier:
                continue
            children=validate_categories(self.catalog.get('children',{}).get(parent,[]))
            child=next((r for r in children if r['id']==identifier),None)
            if not child:
                raise CollectionError('所选下级类目不属于当前上级，请重新读取类目。')
            selected.append(child)
        return Selection(selected[-1]['id'],' > '.join(r['name'] for r in selected),first,
                         str(payload.get('period','7')),str(payload.get('date',''))).validate()

    def save_settings(self,payload):
        selection=self.selection(payload)
        value={**self.config(payload),'root_id':selection.first_category_id,
               'second_id':str(payload.get('second_id','')),'third_id':str(payload.get('third_id','')),
               'period':selection.period,'date':selection.selected_date}
        atomic_json(self.settings_path,value)
        self.settings=value
        return value

    def connection(self,config,fresh=False):
        if (fresh or self.client is None or self.client.profile!=Path(config['profile']).expanduser().resolve()
                or self.client.port!=config['port']):
            self.client=self.client_factory(**config,log=self.log,stop=self.stop)
            self.client.connect()
        return self.client

    def begin(self,kind,action):
        with self.lock:
            if self.closing:
                raise CollectionError('程序正在退出，请重新启动。')
            if self.task['running']:
                raise CollectionError('已有操作正在执行，请等待完成或先停止。')
            self.stop.clear()
            self.task.update(running=True,kind=kind,status='正在准备…',logs=[],error=None,output=None,counts=None)
            def work():
                try:
                    action()
                except (CollectionError,ValueError) as exc:
                    with self.lock:
                        self.task.update(error=str(exc),status=str(exc))
                    self.log(str(exc))
                    self.client=None
                except Exception:
                    message='操作未完成，请检查浏览器登录、网络或文件占用。已完成的JSON和历史Excel仍保留。'
                    with self.lock:
                        self.task.update(error=message,status=message)
                    self.log(message)
                    self.client=None
                finally:
                    with self.lock:
                        self.task['running']=False
            self.worker=threading.Thread(target=work,daemon=True)
            self.worker.start()

    def action(self,route,payload):
        with self.lock:
            if route=='/api/stop':
                self.stop.set()
                self.log('已请求停止，等待当前请求结束并保留已完成数据。')
                return
            if self.task['running']:
                raise CollectionError('已有操作正在执行，请等待完成或先停止。')
            if route=='/api/settings':
                self.save_settings(payload)
                return
            if route=='/api/reveal':
                os.startfile(self.outputs)
                return
            config=self.config(payload)
            if route=='/api/browser':
                def opened():
                    self.connection(config,True)
                    self.log('浏览器已就绪，可正常登录或切换账号。采集时会重新读取会话。')
                self.begin('browser',opened)
            elif route=='/api/categories':
                parent=str(payload.get('parent',''))
                first=parent
                if parent:
                    nodes=self.catalog.get('roots',[])+[n for rows in self.catalog.get('children',{}).values() for n in rows]
                    if not any(str(n.get('id'))==parent for n in nodes):
                        raise CollectionError('请先选择并读取上级类目。')
                    roots={str(n['id']) for n in self.catalog.get('roots',[])}
                    indexed={str(n['id']):n for n in nodes}
                    seen=set()
                    while first not in roots:
                        if first in seen or first not in indexed:
                            raise CollectionError('类目上级关系无效，请重新同步一级类目。')
                        seen.add(first)
                        first=str(indexed[first].get('pid',''))
                def categories():
                    rows=self.connection(config,fresh=not parent).categories(first or None)
                    if not parent and not rows:
                        raise CollectionError('账号未返回可用一级类目，保留已有缓存。')
                    with self.lock:
                        updated=copy.deepcopy(self.catalog) if parent else {'roots':rows,'children':{}}
                        if parent:
                            groups=group_category_tree(rows,first)
                            updated.setdefault('children',{}).update(groups)
                        atomic_json(self.catalog_path,updated)
                        self.catalog=updated
                        self.limits=self.limits_category=None
                    count=len(groups.get(parent,[])) if parent else len(rows)
                    self.log(f'已读取 {count} 个'+('下级类目。' if parent else '一级类目。'))
                self.begin('categories',categories)
            elif route=='/api/time':
                selection=self.selection(payload)
                def limits():
                    info=self.connection(config).limits(selection.category_id)
                    with self.lock:
                        self.limits,self.limits_category=info,selection.category_id
                    self.log('已更新该类目的可用日期。')
                self.begin('time',limits)
            elif route=='/api/start':
                selection=self.selection(payload)
                self.save_settings(payload)
                def collect():
                    path=self.connection(config,True).collect(selection,self.outputs)
                    result=load_json(path.with_suffix('.json'),{})
                    with self.lock:
                        self.task.update(output=path.name,counts={
                            'roots':len(result['roots']['keyWordVoList']),
                            'keywords':len(result['keywords']['hotWordList'])})
                self.begin('collect',collect)
            else:
                raise CollectionError('未知操作。')


class PageSessions:
    """Allow refresh/multiple tabs; stop after the last tab closes or expires."""
    def __init__(self):
        self.created=time.monotonic()
        self.pages={}
        self.closed_at=None
        self.seen=False
        self.lock=threading.Lock()

    def update(self,identifier,closed=False):
        if not isinstance(identifier,str) or not 8<=len(identifier)<=100:
            raise ValueError('页面标识无效。')
        now=time.monotonic()
        with self.lock:
            self.seen=True
            if closed:
                self.pages.pop(identifier,None)
                if not self.pages:
                    self.closed_at=now
            else:
                self.pages[identifier]=now
                self.closed_at=None

    def should_stop(self,now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            self.pages={k:t for k,t in self.pages.items() if now-t<150}
            if self.pages:
                return False
            if not self.seen:
                return now-self.created>60
            if self.closed_at is None:
                self.closed_at=now
            return now-self.closed_at>10


class LocalServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    def __init__(self,address,app):
        self.app=app
        self.token=secrets.token_urlsafe(32)
        self.pages=PageSessions()
        super().__init__(address,Handler)

    def service_actions(self):
        if not self.app.closing and self.pages.should_stop():
            self.app.closing=True
            self.app.stop.set()
            threading.Thread(target=self.shutdown,daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def valid_host(self):
        port=self.server.server_address[1]
        return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}

    def send(self,status,value,mime='application/json; charset=utf-8',filename=None):
        content=json.dumps(value,ensure_ascii=False).encode() if isinstance(value,(dict,list)) else value
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(content)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        if filename:
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def token_valid(self,value=None):
        token=self.headers.get('X-Local-Token','') if value is None else value
        return isinstance(token,str) and hmac.compare_digest(token,self.server.token)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403,{'error':'仅允许本机访问。'})
        url=urlsplit(self.path)
        if url.path=='/':
            html=(ROOT/'web/index.html').read_text(encoding='utf-8').replace('__LOCAL_TOKEN__',self.server.token)
            return self.send(200,html.encode(),'text/html; charset=utf-8')
        if not self.token_valid():
            return self.send(403,{'error':'请刷新本地页面。'})
        if url.path=='/api/state':
            return self.send(200,self.server.app.snapshot())
        if url.path=='/api/download':
            name=parse_qs(url.query).get('file',[''])[0]
            path=(self.server.app.outputs/name).resolve()
            if (Path(name).name!=name or path.parent!=self.server.app.outputs.resolve()
                    or path.suffix!='.xlsx' or not path.is_file() or name.endswith('.tmp.xlsx')):
                return self.send(404,{'error':'结果文件不存在。'})
            return self.send(200,path.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',path.name)
        return self.send(404,{'error':'页面不存在。'})

    def do_POST(self):
        port=self.server.server_address[1]
        allowed={f'http://127.0.0.1:{port}',f'http://localhost:{port}'}
        if not self.valid_host() or self.headers.get('Origin') not in allowed:
            return self.send(403,{'error':'请求来源无效。'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=64000:
                raise ValueError('请求大小无效。')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):
                raise ValueError('请求格式无效。')
            route=urlsplit(self.path).path
            token=payload.get('_token') if route=='/api/page/close' else None
            if not self.token_valid(token):
                return self.send(403,{'error':'请刷新本地页面。'})
            if route in {'/api/page/ping','/api/page/close'}:
                self.server.pages.update(payload.get('page_id'),route.endswith('/close'))
            elif route=='/api/shutdown':
                self.server.app.closing=True
                self.server.app.stop.set()
                threading.Thread(target=self.server.shutdown,daemon=True).start()
            else:
                self.server.app.action(route,payload)
            return self.send(200,{'ok':True})
        except (ValueError,TypeError,OSError) as exc:
            return self.send(400,{'error':str(exc)})


def main():
    parser=argparse.ArgumentParser(description='边界BI本地采集网页')
    parser.add_argument('--port',type=int,default=18766)
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args()
    if not 1024<=args.port<=65535:
        parser.error('网页端口须在1024–65535之间')
    app=LocalApplication()
    try:
        server=LocalServer(('127.0.0.1',args.port),app)
    except OSError:
        print(f'端口已占用。若程序已启动，请打开 http://127.0.0.1:{args.port}；或用 --port 更换端口。')
        return 1
    url=f'http://127.0.0.1:{args.port}'
    print('本地采集页面：'+url,flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()
        if app.worker:
            app.worker.join(65)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
