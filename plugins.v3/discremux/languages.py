"""按 MPLS 主路径 PID 读取语言，不依赖轨道顺序。"""
import ctypes
import ctypes.util
import json
import re
import sys
from pathlib import Path


def parse_mpls(data):
    """解析主视频、主音频和字幕表；跨片段语言冲突时拒绝标注。"""
    def take(pos, size):
        """带边界检查地读取字节。"""
        if pos < 0 or pos + size > len(data):
            raise ValueError('MPLS 数据截断')
        return data[pos:pos + size]
    def integer(pos, size):
        """读取大端整数。"""
        return int.from_bytes(take(pos, size), 'big')
    if take(0, 4) != b'MPLS':
        raise ValueError('不是 MPLS 文件')
    playlist = integer(8, 4)
    count = integer(playlist + 6, 2)
    pos = playlist + 10
    languages = {}
    for _ in range(count):
        length = integer(pos, 2)
        start, end = pos + 2, pos + 2 + length
        take(start, length)
        stn = start + 32
        if integer(start + 10, 1) & 0x10:
            angles = integer(stn, 1)
            stn += 2 + max(0, angles - 1) * 10
        stn_end = stn + 2 + integer(stn, 2)
        if stn_end > end:
            raise ValueError('MPLS STN 越界')
        counts = take(stn + 4, 8)
        entry = stn + 16
        for _ in range(counts[0] + counts[1] + counts[2] + counts[6]):
            n = integer(entry, 1)
            stream = take(entry + 1, n)
            entry += 1 + n
            n = integer(entry, 1)
            attr = take(entry + 1, n)
            entry += 1 + n
            if entry > stn_end:
                raise ValueError('MPLS 轨道越界')
            # 子路径 PID 不属于当前主 TS，不能与主轨道混配。
            if len(stream) < 3 or stream[0] != 1 or not attr:
                continue
            pid = int.from_bytes(stream[1:3], 'big')
            code = attr[0]
            if code in (3, 4, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xa1, 0xa2, 0x92):
                lang = attr[2:5]
            elif code in (0x90, 0x91):
                lang = attr[1:4]
            else:
                continue
            text = lang.decode('ascii', 'replace').lower()
            if re.fullmatch('[a-z]{3}', text) and text != 'und':
                languages.setdefault(pid, set()).add(text)
        pos = end
    return {pid: next(iter(values)) for pid, values in languages.items() if len(values) == 1}


def parse_chapters(data):
    """将 MPLS 入口标记转换为连续播放时间轴上的真实章节。"""
    def integer(pos, size):
        """读取有边界检查的大端字段。"""
        if pos < 0 or pos + size > len(data):
            raise ValueError('MPLS 章节数据截断')
        return int.from_bytes(data[pos:pos + size], 'big')
    if data[:4] != b'MPLS':
        raise ValueError('不是 MPLS')
    playlist, marks = integer(8, 4), integer(12, 4)
    if not marks:
        return []
    count = integer(playlist + 6, 2)
    pos, total, items = playlist + 10, 0, []
    for _ in range(count):
        length = integer(pos, 2)
        start = pos + 2
        if length < 20 or start + length > len(data):
            raise ValueError('MPLS 播放片段长度无效')
        begin, end = integer(start + 12, 4), integer(start + 16, 4)
        if end < begin:
            raise ValueError('MPLS 片段时长无效')
        items.append((total, begin, end))
        total += end - begin
        pos = start + length
    mark_end = marks + 4 + integer(marks, 4)
    count = integer(marks + 4, 2)
    if mark_end > len(data) or marks + 6 + count * 14 > mark_end:
        raise ValueError('MPLS 章节标记越界')
    starts = set()
    for index in range(count):
        pos = marks + 6 + index * 14
        if integer(pos + 1, 1) != 1:
            continue
        ref, timestamp = integer(pos + 2, 2), integer(pos + 4, 4)
        if ref >= len(items):
            raise ValueError('MPLS 章节引用无效')
        offset, begin, end = items[ref]
        if not begin <= timestamp <= end:
            raise ValueError('MPLS 章节时间超出片段')
        value = offset + timestamp - begin
        if value < total:
            starts.add(value)
    starts = sorted(starts)
    return [{'start': value, 'end': starts[i + 1] if i + 1 < len(starts) else total}
            for i, value in enumerate(starts)]


def chapter_metadata(chapters):
    """生成 FFmetadata 章节输入，使用蓝光原生 45kHz 时间基。"""
    lines = [';FFMETADATA1']
    for index, chapter in enumerate(chapters, 1):
        lines.extend(['[CHAPTER]', 'TIMEBASE=1/45000', f"START={chapter['start']}",
                      f"END={chapter['end']}", f'title=Chapter {index:02d}'])
    return chr(10).join(lines) + chr(10)


def read_playlist(source, playlist):
    """目录直接读取，ISO 使用系统 libbluray 的虚拟文件系统读取。"""
    relative = f'BDMV/PLAYLIST/{int(playlist):05d}.mpls'
    path = Path(source)
    if path.is_dir():
        for part in relative.split('/'):
            path = next((p for p in path.iterdir() if p.name.lower() == part.lower()), path / part)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('MPLS 文件过大')
        return path.read_bytes()
    library = ctypes.util.find_library('bluray')
    if not library:
        raise RuntimeError('ISO 语言读取需要系统共享库 libbluray')
    lib = ctypes.CDLL(library)
    lib.bd_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.bd_open.restype = ctypes.c_void_p
    lib.bd_close.argtypes = [ctypes.c_void_p]
    lib.bd_read_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int64)]
    lib.bd_read_file.restype = ctypes.c_int
    libc = ctypes.CDLL(ctypes.util.find_library('c'))
    libc.free.argtypes = [ctypes.c_void_p]
    disc = lib.bd_open(str(path).encode(), None)
    if not disc:
        raise RuntimeError('无法打开 ISO 读取语言')
    pointer, size = ctypes.c_void_p(), ctypes.c_int64()
    try:
        if lib.bd_read_file(disc, relative.encode(), ctypes.byref(pointer), ctypes.byref(size)) != 1:
            raise RuntimeError('无法读取 ISO 中的播放列表')
        if not 0 < size.value <= 16 * 1024 * 1024:
            raise ValueError('MPLS 大小异常')
        return ctypes.string_at(pointer, size.value)
    finally:
        if pointer.value:
            libc.free(pointer)
        lib.bd_close(disc)


def language_args(streams, languages):
    """以输入 PID 匹配语言，以输出流序号设置标签。"""
    args, expected = [], {}
    for index, stream in enumerate(streams):
        language = (stream.get('tags') or {}).get('language', '')
        try:
            pid = int(str(stream.get('id', '')), 0)
        except ValueError:
            pid = -1
        if not language or language == 'und':
            language = languages.get(pid, '')
        if language and language != 'und':
            args.extend([f'-metadata:s:{index}', f'language={language}'])
            expected[index] = language
    return args, expected


if __name__ == '__main__':
    data = read_playlist(sys.argv[1], int(sys.argv[2]))
    print(json.dumps({'languages': parse_mpls(data), 'chapters': parse_chapters(data)}))
