"""测试工具选择及自动安装，不执行真实 apt。"""
import importlib.util
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('runtime', Path(__file__).resolve().parents[1] / 'plugins.v3/discremux/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.report = Mock()

    def test_manual(self):
        config = dict(prefer_system=False, ffmpeg='/custom/ffmpeg', ffprobe='/custom/ffprobe')
        self.assertEqual(runtime.select_tools(config, self.cancel, self.report), ('/custom/ffmpeg', '/custom/ffprobe'))

    def test_existing_never_installs(self):
        with patch.object(runtime, 'system_available', return_value=True), patch.object(runtime, 'apt_run') as apt:
            self.assertEqual(runtime.select_tools({}, self.cancel, self.report), ('/usr/bin/ffmpeg', '/usr/bin/ffprobe'))
            apt.assert_not_called()

    def test_missing_requires_opt_in(self):
        with patch.object(runtime, 'system_available', return_value=False), patch.object(runtime, 'apt_run') as apt:
            with self.assertRaisesRegex(RuntimeError, '开启自动安装'):
                runtime.select_tools({}, self.cancel, self.report)
            apt.assert_not_called()

    def test_install(self):
        with patch.object(runtime, 'system_available', side_effect=[False, False, True]), patch.object(runtime.shutil, 'which', return_value='/usr/bin/apt-get'), patch.object(runtime.os, 'name', 'posix'), patch.object(runtime.os, 'geteuid', return_value=0, create=True), patch.object(runtime, 'apt_run') as apt:
            result = runtime.select_tools({'auto_install': True}, self.cancel, self.report)
            self.assertEqual(result[0], '/usr/bin/ffmpeg')
            self.assertEqual(apt.call_count, 2)
            self.assertEqual(apt.call_args_list[0].args[0][-1], 'update')
            self.assertEqual(apt.call_args_list[1].args[0][-4:], ['install', '-y', '--no-install-recommends', 'ffmpeg'])

    def test_non_root(self):
        with patch.object(runtime, 'system_available', return_value=False), patch.object(runtime.shutil, 'which', return_value='/usr/bin/apt-get'), patch.object(runtime.os, 'name', 'posix'), patch.object(runtime.os, 'geteuid', return_value=1000, create=True), patch.object(runtime, 'apt_run') as apt:
            with self.assertRaisesRegex(RuntimeError, 'root'):
                runtime.select_tools({'auto_install': True}, self.cancel, self.report)
            apt.assert_not_called()

    def test_cancelled(self):
        self.cancel.set()
        with self.assertRaisesRegex(RuntimeError, '停止'):
            runtime.select_tools({}, self.cancel, self.report)

    def test_install_failure_unlocks(self):
        with patch.object(runtime, 'system_available', return_value=False), patch.object(runtime.shutil, 'which', return_value='/usr/bin/apt-get'), patch.object(runtime.os, 'name', 'posix'), patch.object(runtime.os, 'geteuid', return_value=0, create=True), patch.object(runtime, 'apt_run', side_effect=RuntimeError('network')):
            with self.assertRaisesRegex(RuntimeError, 'network'):
                runtime.select_tools({'auto_install': True}, self.cancel, self.report)
        self.assertFalse(runtime._install_lock.locked())
