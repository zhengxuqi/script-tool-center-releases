import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import launcher


class SourceBootstrapTests(unittest.TestCase):
    def test_clean_checkout_uses_tracked_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = {'hub': {'current': '1.0.5', 'previous': None}, 'dmp': {'current': '1.0.2', 'previous': None}}
            (root / 'initial-versions.json').write_text(json.dumps(baseline), encoding='utf-8')
            with patch.object(launcher, 'ROOT', root):
                launcher.initialize_installation()
            self.assertEqual(json.loads((root / 'state/installed.json').read_text()), baseline)

    def test_existing_installation_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'state').mkdir()
            existing = b'{"hub":{"current":"1.0.4","previous":"1.0.3"}}'
            (root / 'state/installed.json').write_bytes(existing)
            with patch.object(launcher, 'ROOT', root):
                launcher.initialize_installation()
            self.assertEqual((root / 'state/installed.json').read_bytes(), existing)

    def test_invalid_baseline_is_rejected_without_writing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'initial-versions.json').write_text('{"hub":{"current":"../wrong"}}', encoding='utf-8')
            with patch.object(launcher, 'ROOT', root), self.assertRaises(ValueError):
                launcher.initialize_installation()
            self.assertFalse((root / 'state/installed.json').exists())


class StartupFallbackTests(unittest.TestCase):
    def setup_case(self, root, minimum):
        installed={'hub':{'current':'1.0.6','previous':'1.0.5'},'dmp':{'current':'1.0.3','previous':'1.0.2'}}
        (root/'state').mkdir()
        (root/'state/installed.json').write_text(json.dumps(installed),encoding='utf-8')
        folder=root/'tools/dmp/versions/1.0.3';folder.mkdir(parents=True)
        (folder/'tool.json').write_text(json.dumps({'id':'dmp','name':'达摩盘','version':'1.0.3','min_hub_version':minimum}),encoding='utf-8')
        return installed

    def test_incompatible_fallback_keeps_registry_and_records_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.setup_case(root,'1.0.6')
            before=(root/'state/installed.json').read_bytes()
            marker=root/'state/user-data-marker';marker.write_text('keep')
            failed=Mock();failed.poll.return_value=1
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'start',return_value=failed) as start,patch.object(launcher,'health',side_effect=[None,None]),patch('sys.argv',['launcher.py','--no-open']):
                with self.assertRaisesRegex(RuntimeError,'无法安全回退.*达摩盘.*1.0.6'):
                    launcher.main()
                start.assert_called_once_with('1.0.6',18760)
            self.assertEqual((root/'state/installed.json').read_bytes(),before)
            self.assertTrue((root/'state/startup-repair.json').is_file())
            self.assertEqual(marker.read_text(),'keep')

    def test_compatible_fallback_still_starts_previous_hub(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.setup_case(root,'1.0.5')
            failed=Mock();failed.poll.return_value=1
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'start',return_value=failed) as start,patch.object(launcher,'health',side_effect=[None,None,{'version':'1.0.5'}]),patch('sys.argv',['launcher.py','--no-open']):
                launcher.main()
                self.assertEqual(start.call_count,2)
            self.assertEqual(json.loads((root/'state/installed.json').read_text())['hub']['current'],'1.0.5')
            self.assertTrue((root/'state/startup-rollback.txt').is_file())

    def test_failed_previous_hub_restores_original_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);original=self.setup_case(root,'1.0.5')
            failed=Mock();failed.poll.return_value=1
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'start',return_value=failed),patch.object(launcher,'health',return_value=None),patch.object(launcher.time,'sleep'),patch('sys.argv',['launcher.py','--no-open']):
                with self.assertRaisesRegex(RuntimeError,'启动与回退均未成功'):
                    launcher.main()
            self.assertEqual(json.loads((root/'state/installed.json').read_text()),original)

    def test_missing_tool_manifest_blocks_unverifiable_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);installed=self.setup_case(root,'1.0.5')
            (root/'tools/dmp/versions/1.0.3/tool.json').unlink()
            with patch.object(launcher,'ROOT',root),self.assertRaisesRegex(RuntimeError,'无法确认'):
                launcher.require_safe_fallback(installed,'1.0.5')


if __name__ == '__main__':
    unittest.main()
