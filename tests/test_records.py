"""通过隔离宿主依赖测试持久记录和重新提取流程。"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'Film.iso'
        self.source.touch()
        self.output = root / 'REMUX'
        modules = {}
        for name in ['app', 'app.sdk', 'app.sdk.plugin', 'app.sdk.logging', 'app.sdk.scheduler']:
            modules[name] = types.ModuleType(name)
        modules['app.sdk.plugin']._PluginBase = object
        modules['app.sdk.logging'].logger = Mock()
        modules['app.sdk.scheduler'].add_plugin_once_job = Mock(return_value=True)
        modules['app.sdk.scheduler'].remove_plugin_once_job = Mock()
        spec = importlib.util.spec_from_file_location('test_discremux', Path(__file__).resolve().parents[1] / 'plugins.v3/discremux/__init__.py', submodule_search_locations=[str(Path(__file__).resolve().parents[1] / 'plugins.v3/discremux')])
        self.module = importlib.util.module_from_spec(spec)
        modules['test_discremux'] = self.module
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.module)
        self.plugin = self.module.DiscRemux()
        self.data = {}
        self.plugin.get_data = self.data.get
        self.plugin.save_data = self.data.__setitem__
        self.plugin._config = {**self.plugin.get_form()[1], 'source': str(self.source), 'output': str(self.output)}
        self.runner = Mock()
        self.runner.remux.return_value = '完成'
        self.tools = patch.object(self.module, 'select_tools', return_value=('ffmpeg', 'ffprobe')).start()
        self.engine = patch.object(self.module, 'RemuxEngine', return_value=self.runner).start()
        self.addCleanup(patch.stopall)

    def test_success_skipped_without_output_and_after_restart(self):
        self.plugin.scan()
        self.assertEqual(self.runner.remux.call_count, 1)
        self.assertTrue(any(key.startswith('completed_') for key in self.data))
        self.data['history'] = []
        self.plugin.scan()
        self.assertEqual(self.runner.remux.call_count, 1)

    def test_failure_retried(self):
        self.runner.remux.side_effect = RuntimeError('failure')
        self.plugin.scan()
        self.plugin.scan()
        self.assertEqual(self.runner.remux.call_count, 2)
        self.assertFalse(any(key.startswith('completed_') for key in self.data))

    def test_legacy_success_migrated(self):
        self.data['history'] = [{'source': str(self.source), 'result': '完成', 'time': '2026-11-09'}]
        self.plugin.scan()
        self.runner.remux.assert_not_called()
        self.tools.assert_not_called()
        self.assertTrue(any(key.startswith('completed_') for key in self.data))

    def test_redo_keeps_existing_output(self):
        self.plugin.scan()
        self.output.mkdir()
        old = self.output / 'Film.mkv'
        old.write_bytes(b'old')
        self.plugin.scan(redo=True)
        self.assertEqual(self.runner.remux.call_count, 2)
        target = self.runner.remux.call_args.args[1]
        self.assertIn('.redo-', target.name)
        self.assertEqual(old.read_bytes(), b'old')

    def test_selector_is_separate(self):
        self.plugin.scan()
        self.plugin._config['selector'] = '800'
        self.plugin.scan()
        self.assertEqual(self.runner.remux.call_count, 2)

    def test_redo_rejects_batch_directory(self):
        self.plugin._config['source'] = str(self.source.parent)
        self.plugin.scan(redo=True)
        self.runner.remux.assert_not_called()
        self.assertIn('单个 ISO', self.plugin._status)
