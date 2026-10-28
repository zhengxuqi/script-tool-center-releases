import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import venv
from pathlib import Path

import psutil
from tool_sdk import atomic_json
from .storage import read_json, version_tuple

SDK = Path(__file__).resolve().parents[1]
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def child_environment():
    result = dict(os.environ)
    result.update(PYTHONPATH=str(SDK), PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return result


class Environments:
    def __init__(self, root):
        self.root = Path(root).resolve() / 'runtime/envs'
        self.lock = threading.Lock()

    def python(self, identifier, version):
        version_tuple(version)
        return self.root / identifier / version / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

    def ensure(self, identifier, version, source, log, cancelled=None):
        executable = self.python(identifier, version)
        marker = executable.parent.parent / 'ready.json'
        requirements = (Path(source) / 'requirements.txt').read_text(encoding='utf-8')
        with self.lock:
            if executable.exists() and read_json(marker, {}).get('requirements') == requirements:
                return executable
            home = executable.parent.parent
            if home.exists():
                # Never remove outside this application's own runtime directory.
                if not home.resolve().is_relative_to(self.root.resolve()) or home.resolve() == self.root.resolve():
                    raise ValueError('运行环境路径无效。')
                shutil.rmtree(home)
            log('正在准备该工具的独立运行环境，首次启动需要下载组件。')
            venv.EnvBuilder(with_pip=True).create(home)
            process = subprocess.Popen([str(executable), '-m', 'pip', 'install', '--disable-pip-version-check',
                '-i', 'https://pypi.org/simple', '-r', str(Path(source) / 'requirements.txt')],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                creationflags=HIDDEN)
            # Do not expose dependency URLs or subprocess command lines in the UI.
            deadline = time.monotonic() + 600
            while True:
                try:
                    output, _ = process.communicate(timeout=.5)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled and cancelled():
                        process.terminate()
                        process.communicate(timeout=10)
                        raise ValueError('运行环境准备已停止。') from None
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        raise ValueError('运行组件下载超时，请检查网络后重试。') from None
            if process.returncode:
                raise ValueError('运行组件安装失败，请检查网络是否能访问 pypi.org 后重试。')
            atomic_json(marker, {'requirements': requirements})
        return executable

    def health(self, identifier, version, source, log):
        executable = self.ensure(identifier, version, source, log)
        entry = 'hub.py' if identifier == 'hub' else 'adapter.py'
        try:
            result = subprocess.run([str(executable), '-X', 'utf8', str(Path(source) / entry), '--health-check'],
                cwd=source, env=child_environment(), capture_output=True, timeout=60, creationflags=HIDDEN)
        except subprocess.TimeoutExpired:
            raise ValueError('新版本启动检查超时，当前版本已保留。') from None
        if result.returncode:
            raise ValueError('新版本启动检查失败，当前版本已保留。')
        return executable


class TaskQueue:
    def __init__(self, store, environments):
        self.store, self.environments = store, environments
        self.gate = threading.RLock()
        self.pending = queue.Queue()
        self.maintenance = False
        self.closing = False
        self.process = None
        self._recover()
        self.worker = threading.Thread(target=self._consume, daemon=True)
        self.worker.start()

    def _recover(self):
        for job in self.store.unfinished():
            # An old worker may survive an abrupt host exit. Only stop our exact
            # recorded adapter process, never Chrome or an unrelated reused PID.
            if job.get('pid') and job.get('process_created'):
                try:
                    process = psutil.Process(job['pid'])
                    args = process.cmdline()
                    request = str(self.store.state / 'jobs' / job['id'] / 'request.json')
                    if process.create_time() == job['process_created'] and request in args and '--request' in args:
                        children = []
                        for child in process.children(recursive=True):
                            try:
                                command = child.cmdline()
                                if request in command and '--request' in command:
                                    children.append(child)
                            except psutil.Error:
                                pass
                        for child in children:
                            try:
                                child.terminate()
                            except psutil.Error:
                                pass
                        process.terminate()
                        process.wait(timeout=10)
                        psutil.wait_procs(children,timeout=10)
                except (psutil.Error, OSError):
                    pass
            job.update(status='interrupted', message='上次运行被中断，已保留结果，可重新运行或继续采集。', finished=time.time())
            self.store.put_job(job)

    def submit(self, identifier, action, params, resume=None):
        with self.gate:
            if self.maintenance or self.closing:
                raise ValueError('正在更新或退出，请稍后再提交任务。')
            tool = self.store.tool(identifier)
            if tool.get('runtime') == 'node':
                raise ValueError('请在原评论采集页面启动此工具的采集。')
            if action not in tool['actions']:
                raise ValueError('工具不支持此操作。')
            if not isinstance(params, dict):
                raise ValueError('任务参数格式无效。')
            resume_path = None
            if resume:
                previous = self.store.job(resume)
                if previous['status'] in {'queued', 'running', 'stopping'}:
                    raise ValueError('请先等待原任务结束。')
                if previous['tool_id'] != identifier or not tool.get('supports_resume') or previous['action'] != 'collect':
                    raise ValueError('这项任务不支持继续采集。')
                resume_path = self.store.state / 'jobs' / previous['id'] / 'output/progress.json'
                if not resume_path.is_file():
                    resume_path = self.store.outputs / identifier / previous['id'] / 'progress.json'
                if not resume_path.is_file():
                    raise ValueError('没有可继续的采集进度。')
                params = previous['params']
            identifier_job = uuid.uuid4().hex
            job = {'id': identifier_job, 'tool_id': identifier, 'action': action, 'params': params,
                   'browser': {key: self.store.config[key] for key in ('profile', 'port')},
                   'version': tool['version'], 'created': time.time(), 'status': 'queued',
                   'message': '等待前面的任务完成', 'logs': [], 'completed': 0, 'total': 0,
                   'resume_path': str(resume_path) if resume_path else None}
            self.store.put_job(job)
            self.pending.put(identifier_job)
            return job

    def stop(self, identifier):
        with self.gate:
            job = self.store.job(identifier)
            if job['status'] == 'queued':
                job.update(status='cancelled', finished=time.time(), message='已取消排队')
            elif job['status'] in {'running', 'stopping'}:
                stop = self.store.state / 'jobs' / identifier / 'stop'
                stop.parent.mkdir(parents=True, exist_ok=True)
                stop.touch()
                job.update(status='stopping', message='正在停止，已完成的数据会保留')
            self.store.put_job(job)
        if job['params'].get('_portal_ticket') and getattr(self, 'dmp_portal', None):
            self.dmp_portal.cancel(job)

    def _consume(self):
        while True:
            identifier = self.pending.get()
            try:
                self._execute(identifier)
            except Exception:
                job = self.store.job(identifier)
                job.update(status='failed', message='任务未完成，请检查浏览器或运行环境后重试。', finished=time.time())
                self.store.put_job(job)
            finally:
                self.pending.task_done()

    def _execute(self, identifier):
        while True:
            with self.gate:
                job = self.store.job(identifier)
                if job['status'] != 'queued':
                    return
                portal = getattr(self, 'pdd_portal', None)
                waiting = portal and portal.info().get('busy')
                if not waiting:
                    job.update(status='running', started=time.time(), message='正在准备')
                    self.store.put_job(job)
                    break
                if job['message'] != '等待拼多多采集结束，共享浏览器按顺序使用':
                    job['message'] = '等待拼多多采集结束，共享浏览器按顺序使用'
                    self.store.put_job(job)
            time.sleep(.3)
        def log(message):
            with self.gate:
                latest = self.store.job(identifier)
                latest['logs'] = (latest['logs'] + [str(message)[:1500]])[-150:]
                latest['message'] = str(message)[:1500]
                self.store.put_job(latest)
        if job['params'].get('_portal_ticket') and job['tool_id'] == 'dmp':
            if not getattr(self, 'dmp_portal', None):
                raise ValueError('达摩盘原页面接入服务尚未就绪。')
            self.dmp_portal.run_job(job)
            return
        source = self.store.version_path(job['tool_id'], job['version'])
        try:
            python = self.environments.ensure(job['tool_id'], job['version'], source, log,
                      cancelled=lambda: self.store.job(identifier)['status'] == 'stopping')
        except ValueError as exc:
            with self.gate:
                latest = self.store.job(identifier)
                latest.update(status='cancelled' if latest['status'] == 'stopping' else 'failed',
                              message=str(exc), finished=time.time())
                self.store.put_job(latest)
            return
        if self.store.job(identifier)['status'] == 'stopping':
            job = self.store.job(identifier)
            job.update(status='cancelled', message='已停止', finished=time.time())
            self.store.put_job(job)
            return
        request_dir = self.store.state / 'jobs' / identifier
        output = request_dir / 'output'
        output.mkdir(parents=True, exist_ok=True)
        request = {**job, 'data_dir': str(self.store.state / 'tools' / job['tool_id']),
                   'output_dir': str(output), 'stop_file': str(request_dir / 'stop')}
        request_path = request_dir / 'request.json'
        atomic_json(request_path, request)
        process = subprocess.Popen([str(python), '-X', 'utf8', '-u', str(source / 'adapter.py'),
                    '--request', str(request_path)], cwd=source, env=child_environment(),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding='utf-8', errors='replace',
                    creationflags=HIDDEN)
        self.process = process
        with self.gate:
            job = self.store.job(identifier)
            job.update(pid=process.pid, process_created=psutil.Process(process.pid).create_time())
            self.store.put_job(job)
        got_result, error = False, None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            with self.gate:
                job = self.store.job(identifier)
                if event.get('type') == 'log':
                    log(event['message'])
                    continue
                if event.get('type') == 'progress':
                    job.update(completed=int(event['completed']), total=int(event['total']))
                elif event.get('type') == 'result':
                    got_result = True
                    job['result'] = event.get('value', {})
                elif event.get('type') == 'error':
                    error = str(event.get('message', '操作失败'))[:1500]
                self.store.put_job(job)
        process.stdout.close()
        returncode = process.wait()
        published = self.store.publish_workspace(job['tool_id'], output)
        with self.gate:
            job = self.store.job(identifier)
            if published:
                result = job.get('result') if isinstance(job.get('result'), dict) else {}
                result['filename'] = published.get(result.get('filename'), result.get('filename'))
                result['files'] = list(published.values())
                job['result'] = result
            cancelled = job['status'] == 'stopping'
            success = got_result and returncode == 0 and not error
            status = 'cancelled' if cancelled else 'completed' if success else 'failed'
            message = '已停止，采集进度与结果已保留' if cancelled else error or ('已完成' if success else '工具进程未正常完成，请检查登录状态或运行组件。')
            job.update(status=status, message=message, finished=time.time())
            self.store.put_job(job)
            self.process = None

    def reserve_update(self):
        with self.gate:
            if self.maintenance or self.store.unfinished():
                raise ValueError('请先等待任务队列结束，再安装或回退版本。')
            portal = getattr(self, 'pdd_portal', None)
            if portal:
                portal.stop()
            self.maintenance = True

    def release_update(self):
        with self.gate:
            self.maintenance = False
