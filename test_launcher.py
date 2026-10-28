import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
