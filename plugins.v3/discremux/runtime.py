"""选择及按需安装容器系统 FFmpeg。"""
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_install_lock = threading.Lock()


def system_available():
    """检查系统工具是否成对存在且可执行。"""
    return all(Path(p).is_file() and os.access(p, os.X_OK)
               for p in ('/usr/bin/ffmpeg', '/usr/bin/ffprobe'))


def apt_run(args):
    """执行固定 apt 命令，将输出写入临时文件以限制内存占用。"""
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    with tempfile.TemporaryFile() as log:
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, env=env, timeout=900)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('系统 FFmpeg 安装超时，请检查软件源网络及包管理器状态') from exc
        if result.returncode:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 4000))
            detail = ' | '.join(log.read().decode('utf-8', 'replace').splitlines())
            raise RuntimeError(f'apt 退出码 {result.returncode}：{detail}')


def select_tools(config, cancelled, report):
    """系统模式下复用已安装工具，获准时按需安装，手动模式保留用户路径。"""
    if not config.get('prefer_system', True):
        return config['ffmpeg'], config['ffprobe']
    if cancelled.is_set():
        raise RuntimeError('任务已停止')
    if not system_available():
        if not config.get('auto_install', False):
            raise RuntimeError('未找到 /usr/bin/ffmpeg 和 ffprobe；请开启自动安装系统版 FFmpeg，或关闭优先系统版并填写自定义路径')
        apt = shutil.which('apt-get')
        if os.name != 'posix' or not apt:
            raise RuntimeError('自动安装仅支持具有 apt-get 的 Debian/Ubuntu 环境；请关闭优先系统版并配置自定义工具')
        if os.geteuid() != 0:
            raise RuntimeError('自动安装需要容器内 root 权限；当前用户不能安装系统软件')
        if not _install_lock.acquire(blocking=False):
            raise RuntimeError('另一个原盘提取实例正在安装 FFmpeg，请稍后重试')
        try:
            if not system_available():
                report('正在安装系统版 FFmpeg；软件包安装期间停止请求将在当前 apt 步骤结束后生效')
                opts = ['-o', 'DPkg::Lock::Timeout=60', '-o', 'Acquire::Retries=2',
                        '-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30']
                apt_run([apt, *opts, 'update'])
                if cancelled.is_set():
                    raise RuntimeError('任务已停止')
                apt_run([apt, *opts, 'install', '-y', '--no-install-recommends', 'ffmpeg'])
        finally:
            _install_lock.release()
        if not system_available():
            raise RuntimeError('安装完成但未找到系统 FFmpeg/FFprobe，请检查软件包状态')
    if cancelled.is_set():
        raise RuntimeError('任务已停止')
    report('使用系统工具：/usr/bin/ffmpeg、/usr/bin/ffprobe')
    return '/usr/bin/ffmpeg', '/usr/bin/ffprobe'
