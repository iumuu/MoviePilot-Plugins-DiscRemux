"""原盘导航、无损封装与安全输出。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path


def disc_kind(path: Path, iso_kind: str = 'bluray') -> str | None:
    """根据结构识别原盘；ISO 类型由用户明确指定。"""
    if path.is_file() and path.suffix.lower() == '.iso':
        return iso_kind
    if path.is_dir():
        names = {p.name.upper() for p in path.iterdir()}
        if 'BDMV' in names:
            return 'bluray'
        if 'VIDEO_TS' in names:
            return 'dvd'
    return None


def discover(root: Path, iso_kind: str = 'bluray'):
    """遍历原盘根目录，发现原盘后不再进入其内部。"""
    if disc_kind(root, iso_kind):
        yield root
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        if disc_kind(base, iso_kind):
            dirs[:] = []
            yield base
            continue
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        for name in sorted(files):
            item = base / name
            if item.suffix.lower() == '.iso' and not item.is_symlink():
                yield item


def input_args(source: Path, kind: str, selector: str = '') -> list[str]:
    """通过导航读取完整播放列表，避免最大文件截断正片。"""
    if selector and (not selector.isdecimal() or int(selector) < 0):
        raise ValueError('播放列表/标题必须为非负整数')
    if kind == 'bluray':
        return (['-playlist', str(int(selector))] if selector else []) + ['-i', 'bluray:' + str(source)]
    if kind == 'dvd':
        if selector and int(selector) < 1:
            raise ValueError('DVD 标题必须从 1 开始')
        return ['-f', 'dvdvideo', '-title', selector or '1', '-i', str(source)]
    raise ValueError('ISO 类型必须为 bluray 或 dvd')


class RemuxEngine:
    """可取消的 FFmpeg 执行器，验证后再提交输出。"""

    def __init__(self, ffmpeg='ffmpeg', ffprobe='ffprobe', timeout=43200):
        """初始化工具路径及最长任务时长。"""
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.timeout = timeout
        self.cancel = threading.Event()

    def run(self, args: list[str]) -> str:
        """使用文件承接子进程输出，防止管道阻塞并支持中止。"""
        if self.cancel.is_set():
            raise RuntimeError('任务已停止')
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    if self.cancel.wait(0.2):
                        raise RuntimeError('任务已停止')
                    if time.monotonic() - started > self.timeout:
                        raise TimeoutError('FFmpeg/FFprobe 超时')
                if proc.returncode:
                    err.seek(0, 2)
                    err.seek(max(0, err.tell() - 6000))
                    raise RuntimeError(err.read().decode('utf-8', 'replace'))
                out.seek(0)
                return out.read().decode('utf-8', 'replace')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

    def check(self, kind: str):
        """检查两种工具的导航能力，避免运行到中途才发现缺失依赖。"""
        for tool in (self.ffmpeg, self.ffprobe):
            flag, feature = ('-protocols', 'bluray') if kind == 'bluray' else ('-demuxers', 'dvdvideo')
            if feature not in self.run([tool, '-v', 'error', flag]).split():
                raise RuntimeError(f'{tool} 缺少 {feature}；请安装启用 libbluray 或 libdvdnav/libdvdread 的 FFmpeg')

    def probe(self, args: list[str]) -> dict:
        """探测所选标题的轨道、时长和章节。"""
        return json.loads(self.run([self.ffprobe, '-v', 'error', *args, '-show_streams', '-show_format', '-show_chapters', '-of', 'json']))

    def remux(self, source: Path, target: Path, kind: str, selector='') -> str:
        """逐轨复制视频音频字幕，校验完整性后原子发布且不覆盖已有文件。"""
        if target.exists():
            return '已跳过：目标已存在'
        args = input_args(source, kind, selector)
        original = self.probe(args)
        streams = [s for s in original.get('streams', []) if s.get('codec_type') in ('video', 'audio', 'subtitle')]
        if not any(s['codec_type'] == 'video' for s in streams):
            raise RuntimeError('未找到视频轨道')
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = target.with_suffix(target.suffix + '.lock')
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        temp = None
        try:
            if target.exists():
                return '已跳过：目标已存在'
            fd, name = tempfile.mkstemp(prefix='.' + target.stem + '.', suffix='.partial.mkv', dir=target.parent)
            os.close(fd)
            temp = Path(name)
            maps = [arg for s in streams for arg in ('-map', '0:' + str(s['index']))]
            self.run([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args, *maps, '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy', '-max_muxing_queue_size', '4096', '-f', 'matroska', str(temp)])
            actual = self.probe(['-i', str(temp)])
            expected_tracks = Counter((s['codec_type'], s['codec_name']) for s in streams)
            actual_tracks = Counter((s['codec_type'], s['codec_name']) for s in actual.get('streams', []))
            if expected_tracks != actual_tracks:
                raise RuntimeError('输出轨道数量或编码与原盘不一致')
            duration = float(original.get('format', {}).get('duration', 0))
            output_duration = float(actual.get('format', {}).get('duration', 0))
            if duration > 0 and abs(duration - output_duration) > max(3, duration * 0.001):
                raise RuntimeError('输出时长与原盘不一致')
            if len(original.get('chapters', [])) != len(actual.get('chapters', [])):
                raise RuntimeError('输出章节数量与原盘不一致')
            if self.cancel.is_set():
                raise RuntimeError('任务已停止')
            # 同目录硬链接原子创建最终名称；已存在时失败，永不覆盖。
            os.link(temp, target)
            return '完成'
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
