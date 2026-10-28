import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app/versions/1.0.3'))
from hubcore.shared_browser import ensure_shared_browser
import psutil


class SharedBrowserTests(unittest.TestCase):
    profile = str((ROOT / 'analysis/test-shared-profile').resolve())
    chrome = r'C:\Program Files\Google\Chrome\Application\chrome.exe'

    def connection(self):
        return SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9351), pid=123)

    @patch('hubcore.shared_browser.subprocess.Popen')
    @patch('hubcore.shared_browser.psutil.Process')
    @patch('hubcore.shared_browser.psutil.net_connections')
    def test_wrong_profile_port_is_rejected_without_launching(self, connections, process, launch):
        connections.return_value = [self.connection()]
        process.return_value.cmdline.return_value = [self.chrome, '--user-data-dir=' + str(ROOT / 'different-profile')]
        with self.assertRaisesRegex(ValueError, '其他程序或用户目录'):
            ensure_shared_browser(self.profile, 9351)
        launch.assert_not_called()

    @patch('hubcore.shared_browser.requests.Session')
    @patch('hubcore.shared_browser.subprocess.Popen')
    @patch('hubcore.shared_browser.psutil.Process')
    @patch('hubcore.shared_browser.psutil.net_connections')
    def test_existing_matching_chrome_is_reused(self, connections, process, launch, session):
        connections.return_value = [self.connection()]
        process.return_value.cmdline.return_value = [self.chrome, '--user-data-dir', self.profile]
        http = session.return_value.__enter__.return_value
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = {'webSocketDebuggerUrl': 'ws://127.0.0.1:9351/devtools/browser/test'}
        self.assertEqual(ensure_shared_browser(self.profile, 9351), 'http://127.0.0.1:9351')
        launch.assert_not_called()
        self.assertFalse(http.trust_env)

    @patch('hubcore.shared_browser.subprocess.Popen')
    @patch('hubcore.shared_browser.psutil.process_iter')
    @patch('hubcore.shared_browser.psutil.net_connections', return_value=[])
    def test_locked_profile_without_debug_port_is_preserved(self, connections, processes, launch):
        processes.return_value = [SimpleNamespace(info={'cmdline': [self.chrome, '--user-data-dir=' + self.profile]})]
        with self.assertRaisesRegex(ValueError, '正常关闭'):
            ensure_shared_browser(self.profile, 9351)
        launch.assert_not_called()


class SharedQueueTests(unittest.TestCase):
    def test_queue_waits_for_pdd_and_waiting_jobs_remain_cancellable(self):
        from test_hub import seed, eventually
        from hubcore.runtime import TaskQueue
        with tempfile.TemporaryDirectory() as directory:
            store = seed(Path(directory))
            env = Mock()
            env.ensure.return_value = Path(sys.executable)
            queue = TaskQueue(store, env)
            portal = Mock()
            portal.info.return_value = {'busy': True}
            queue.pdd_portal = portal
            first = queue.submit('boundary', 'collect', {'steps': 1})
            second = queue.submit('dmp', 'collect', {'steps': 1})
            eventually(lambda: store.job(first['id'])['message'].startswith('等待拼多多'))
            self.assertEqual(store.job(first['id'])['status'], 'queued')
            env.ensure.assert_not_called()
            queue.stop(second['id'])
            self.assertEqual(store.job(second['id'])['status'], 'cancelled')
            portal.info.return_value = {'busy': False}
            eventually(lambda: store.job(first['id'])['status'] == 'completed')
            env.ensure.assert_called_once()


if __name__ == '__main__':
    unittest.main()
