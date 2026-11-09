"""运行：python -m unittest discover -s tests -v。"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('engine', Path(__file__).resolve().parents[1] / 'plugins.v3/discremux/engine.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = engine.RemuxEngine()
        self.media = {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264'}, {'index': 1, 'codec_type': 'audio', 'codec_name': 'ac3'}, {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle'}], 'format': {'duration': '120'}, 'chapters': [{'id': 0}]}

    def test_discovery_stops_at_disc(self):
        (self.root / 'movie/BDMV/STREAM').mkdir(parents=True)
        (self.root / 'movie/BDMV/nested.iso').touch()
        (self.root / 'second.ISO').touch()
        self.assertEqual(set(engine.discover(self.root)), {self.root / 'movie', self.root / 'second.ISO'})

    def test_directory_types(self):
        (self.root / 'VIDEO_TS').mkdir()
        self.assertEqual(engine.disc_kind(self.root), 'dvd')

    def test_nested_output_excluded(self):
        output = self.root / 'REMUX'
        (output / 'nested/BDMV').mkdir(parents=True)
        (output / 'ignored.iso').touch()
        (self.root / 'original.iso').touch()
        (self.root / 'REMUX-other').mkdir()
        (self.root / 'REMUX-other/keep.iso').touch()
        engine.validate_output(self.root, output)
        self.assertEqual(set(engine.discover(self.root, exclude=output)),
                         {self.root / 'original.iso', self.root / 'REMUX-other/keep.iso'})

    def test_output_not_yet_created(self):
        (self.root / 'original.iso').touch()
        output = self.root / 'new/REMUX'
        engine.validate_output(self.root, output)
        self.assertEqual(list(engine.discover(self.root, exclude=output)), [self.root / 'original.iso'])

    def test_output_path_validation(self):
        for output in [self.root, self.root.parent, self.root / 'REMUX/..']:
            with self.assertRaises(ValueError):
                engine.validate_output(self.root, output)
        engine.validate_output(self.root, self.root.parent / 'separate-output')

    def test_bluray_navigation(self):
        args = engine.input_args(self.root, 'bluray', '00800')
        self.assertEqual(args, ['-playlist', '800', '-i', 'bluray:' + str(self.root)])

    def test_dvd_navigation(self):
        self.assertEqual(engine.input_args(self.root, 'dvd', '2')[:4], ['-f', 'dvdvideo', '-title', '2'])
        with self.assertRaises(ValueError):
            engine.input_args(self.root, 'dvd', '0')

    def test_invalid_selector(self):
        for selector in ['-1', '../x', '1;echo x']:
            with self.assertRaises(ValueError):
                engine.input_args(self.root, 'bluray', selector)

    def test_success_copies_all_tracks(self):
        target = self.root / 'film.mkv'
        with patch.object(self.runner, 'probe', return_value=self.media), patch.object(self.runner, 'run') as run:
            self.assertEqual(self.runner.remux(self.root, target, 'bluray'), '完成')
            cmd = run.call_args.args[0]
            self.assertIn('copy', cmd)
            self.assertEqual(cmd.count('-map'), 3)
            self.assertIn('-map_chapters', cmd)
        self.assertTrue(target.exists())
        self.assertFalse(list(self.root.glob('*.lock')))
        self.assertFalse(list(self.root.glob('*.partial.mkv')))

    def test_existing_output_untouched(self):
        target = self.root / 'film.mkv'
        target.write_bytes(b'original')
        with patch.object(self.runner, 'probe') as probe:
            self.runner.remux(self.root, target, 'bluray')
            probe.assert_not_called()
        self.assertEqual(target.read_bytes(), b'original')

    def test_failures_cleanup(self):
        for actual in [dict(self.media, streams=self.media['streams'][:1]), dict(self.media, format={'duration': '60'}), dict(self.media, chapters=[])]:
            target = self.root / 'film.mkv'
            with patch.object(self.runner, 'probe', side_effect=[self.media, actual]), patch.object(self.runner, 'run'):
                with self.assertRaises(RuntimeError):
                    self.runner.remux(self.root, target, 'bluray')
            self.assertEqual(list(self.root.iterdir()), [])

    def test_ffmpeg_failure_cleanup(self):
        with patch.object(self.runner, 'probe', return_value=self.media), patch.object(self.runner, 'run', side_effect=RuntimeError('unsupported subtitle')):
            with self.assertRaises(RuntimeError):
                self.runner.remux(self.root, self.root / 'film.mkv', 'bluray')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lock_preserves_other_job(self):
        lock = self.root / 'film.mkv.lock'
        lock.touch()
        with patch.object(self.runner, 'probe', return_value=self.media):
            with self.assertRaises(FileExistsError):
                self.runner.remux(self.root, self.root / 'film.mkv', 'bluray')
        self.assertTrue(lock.exists())

    def test_cancel(self):
        self.runner.cancel.set()
        with self.assertRaises(RuntimeError):
            self.runner.run([sys.executable, '-c', 'pass'])

    def test_process_errors_and_timeout(self):
        with self.assertRaisesRegex(RuntimeError, 'example error'):
            self.runner.run([sys.executable, '-c', 'import sys; sys.stderr.write("example error"); sys.exit(1)'])
        self.runner.timeout = 0.1
        with self.assertRaises(TimeoutError):
            self.runner.run([sys.executable, '-c', 'import time; time.sleep(10)'])

    def test_capability_missing(self):
        with patch.object(self.runner, 'run', return_value='file http'):
            with self.assertRaisesRegex(RuntimeError, 'libbluray'):
                self.runner.check('bluray')

    def test_metadata(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'package.v3.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['DiscRemux']['version'], '1.1.0')


if __name__ == '__main__':
    unittest.main()
