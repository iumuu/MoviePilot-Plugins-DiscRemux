"""使用合成 MPLS 验证 PID 语言解析及错配保护。"""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('languages', Path(__file__).resolve().parents[1] / 'plugins.v3/discremux/languages.py')
languages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(languages)


def fixture(lang=b'eng'):
    """构造一条音频与一条 PGS 字幕的主播放片段。"""
    def stream(pid, attr):
        """编码轨道条目。"""
        return bytes([3, 1]) + pid.to_bytes(2, 'big') + bytes([len(attr)]) + attr
    entries = stream(0x1100, bytes([0x86, 0]) + lang) + stream(0x1200, bytes([0x90]) + b'zho')
    stn = bytes([0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]) + entries
    item = bytes(32) + len(stn).to_bytes(2, 'big') + stn
    playlist = bytes(2) + bytes([0, 1, 0, 0]) + len(item).to_bytes(2, 'big') + item
    return b'MPLS0200' + (20).to_bytes(4, 'big') + bytes(8) + len(playlist).to_bytes(4, 'big') + playlist


class LanguageTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(languages.parse_mpls(fixture()), {0x1100: 'eng', 0x1200: 'zho'})

    def test_truncated(self):
        with self.assertRaises(ValueError):
            languages.parse_mpls(fixture()[:-1])

    def test_pid_not_order(self):
        streams = [{'id': '0x1200'}, {'id': '0x1100'}, {'id': '0x1101'}]
        args, expected = languages.language_args(streams, {0x1100: 'eng', 0x1200: 'zho'})
        self.assertEqual(expected, {0: 'zho', 1: 'eng'})
        self.assertEqual(args[:2], ['-metadata:s:0', 'language=zho'])

    def test_existing_language(self):
        _, expected = languages.language_args([{'id': '0x1100', 'tags': {'language': 'fra'}}], {0x1100: 'eng'})
        self.assertEqual(expected, {0: 'fra'})

    def test_unknown_not_invented(self):
        _, expected = languages.language_args([{'id': 'N/A', 'tags': {'language': 'und'}}], {0x1100: 'eng'})
        self.assertEqual(expected, {})

    def test_chapters_across_clips(self):
        items = []
        for begin, end in [(450000, 900000), (90000, 990000)]:
            item = bytearray(32)
            item[12:16] = begin.to_bytes(4, 'big')
            item[16:20] = end.to_bytes(4, 'big')
            items.append(len(item).to_bytes(2, 'big') + item)
        playlist = bytes(2) + (2).to_bytes(2, 'big') + bytes(2) + b''.join(items)
        marks = []
        for ref, timestamp in [(0, 450000), (1, 315000)]:
            marks.append(bytes([0, 1]) + ref.to_bytes(2, 'big') + timestamp.to_bytes(4, 'big') + bytes(6))
        mark_data = (2).to_bytes(2, 'big') + b''.join(marks)
        mark_offset = 20 + 4 + len(playlist)
        data = b'MPLS0200' + (20).to_bytes(4, 'big') + mark_offset.to_bytes(4, 'big') + bytes(4) + len(playlist).to_bytes(4, 'big') + playlist + len(mark_data).to_bytes(4, 'big') + mark_data
        chapters = languages.parse_chapters(data)
        self.assertEqual(chapters, [{'start': 0, 'end': 675000}, {'start': 675000, 'end': 1350000}])
        text = languages.chapter_metadata(chapters)
        self.assertEqual(text.count('[CHAPTER]'), 2)
        self.assertIn('TIMEBASE=1/45000', text)
        with self.assertRaises(ValueError):
            languages.parse_chapters(data[:-1])

    def test_no_marks(self):
        self.assertEqual(languages.parse_chapters(fixture()), [])
