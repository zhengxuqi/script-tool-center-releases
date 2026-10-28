import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app/versions/1.0.6'))
from tool_sdk import DEFAULT_BROWSER_PROFILE, ensure_browser_directory, atomic_json
from hubcore.storage import Store


class BrowserDirectoryTests(unittest.TestCase):
    def test_missing_directory_and_parents_created_without_overwriting_data(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'new-parent/User Data'
            self.assertFalse(profile.exists())
            self.assertEqual(ensure_browser_directory(profile), profile.resolve())
            marker = profile / 'existing-login-marker'
            marker.write_text('keep', encoding='utf-8')
            ensure_browser_directory(profile)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_default_used_when_config_is_missing_or_blank(self):
        self.assertEqual(DEFAULT_BROWSER_PROFILE, r'D:\ChromeDebug\User Data')
        for saved in ({'repository': 'example/releases'}, {'profile': ''}, {'profile': None}):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                atomic_json(root / 'state/config.json', saved)
                with patch('hubcore.storage.ensure_browser_directory') as ensure:
                    store = Store(root)
                    ensure.assert_called_once_with(DEFAULT_BROWSER_PROFILE)
                self.assertEqual(store.config['profile'], DEFAULT_BROWSER_PROFILE)
                self.assertEqual(store.config['port'], 9351)
                self.assertEqual(json.loads((root / 'state/config.json').read_text())['profile'], DEFAULT_BROWSER_PROFILE)

    def test_setting_custom_directory_creates_it_and_preserves_current_config_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / 'custom-profile/nested'
            with patch('hubcore.storage.ensure_browser_directory'):
                store = Store(root)
            store.save_config({'profile': str(profile), 'port': 9351, 'repository': ''})
            self.assertTrue(profile.is_dir())
            before = (root / 'state/config.json').read_bytes()
            blocker = root / 'ordinary-file'
            blocker.write_text('keep', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '无法创建'):
                store.save_config({'profile': str(blocker), 'port': 9351})
            self.assertEqual((root / 'state/config.json').read_bytes(), before)
            self.assertEqual(blocker.read_text(encoding='utf-8'), 'keep')

    def test_creation_error_is_actionable_and_settings_remain_available(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('hubcore.storage.ensure_browser_directory', side_effect=ValueError('无法创建浏览器用户目录：盘符不存在')):
                store = Store(Path(temp))
            self.assertIn('无法创建', store.browser_directory_error)
            self.assertEqual(store.config['profile'], DEFAULT_BROWSER_PROFILE)
        with patch.object(Path, 'mkdir', side_effect=PermissionError()):
            with self.assertRaisesRegex(ValueError, '盘符和写入权限'):
                ensure_browser_directory(DEFAULT_BROWSER_PROFILE)


if __name__ == '__main__':
    unittest.main()
