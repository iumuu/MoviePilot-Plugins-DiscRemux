"""原盘导航、无损封装与安全输出。"""
from __future__ import annotations

import json
import os
import shlex
import signal
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


def validate_output(root: Path, output: Path):
    """允许输出位于扫描目录内，但禁止同目录或输出包含输入。"""
    root, output = root.resolve(), output.resolve()
    if root == output or root.is_relative_to(output):
        raise ValueError('输出目录不能与输入相同，也不能是输入的父目录；可以使用输入下的 REMUX 子目录')


def discover(root: Path, iso_kind: str = 'bluray', exclude: Path | None = None):
    """遍历原盘根目录，跳过输出子树，发现原盘后不再进入其内部。"""
    root = root.resolve()
    excluded = exclude.resolve() if exclude is not None else None
    if excluded is not None and root.is_relative_to(excluded):
        return
    if disc_kind(root, iso_kind):
        yield root
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        if excluded is not None and base.resolve().is_relative_to(excluded):
            dirs[:] = []
            continue
        if disc_kind(base, iso_kind):
            dirs[:] = []
            yield base
            continue
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()
                   and (excluded is None or not (base / d).resolve().is_relative_to(excluded))]
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
                    size = err.tell()
                    err.seek(0)
                    details = err.read(2000)
                    if size > 2000:
                        err.seek(max(2000, size - 4000))
                        details += b'\n... [stderr tail] ...\n' + err.read()
                    reason = f'退出码 {proc.returncode}'
                    if os.name == 'posix' and proc.returncode < 0:
                        try:
                            reason += f' ({signal.Signals(-proc.returncode).name})'
                        except ValueError:
                            pass
                    command = shlex.join(args)
                    diagnostic = details.decode('utf-8', 'replace').strip() or '子进程没有输出错误详情'
                    diagnostic = ' | '.join(diagnostic.splitlines())
                    raise RuntimeError(f'{Path(args[0]).name} {reason}; 命令: {command}; stderr: {diagnostic}')
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
        return json.loads(self.run([self.ffprobe, '-hide_banner', '-v', 'info', *args, '-show_streams', '-show_format', '-show_chapters', '-of', 'json']))

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
            self.run([self.ffmpeg, '-hide_banner', '-loglevel', 'info', '-nostats', '-nostdin', '-y', *args, *maps, '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy', '-max_muxing_queue_size', '4096', '-f', 'matroska', str(temp)])
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
