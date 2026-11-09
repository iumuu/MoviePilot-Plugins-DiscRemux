"""MoviePilot v3 原盘无损提取插件。"""
import threading
from datetime import datetime
from pathlib import Path

from app.sdk.plugin import _PluginBase
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .engine import RemuxEngine, discover, disc_kind, validate_output


class DiscRemux(_PluginBase):
    """定时扫描原盘并无损封装 MKV。"""
    plugin_name = '原盘无损提取'
    plugin_desc = '使用 FFmpeg 将蓝光/DVD 目录及 ISO 无损封装为 MKV。'
    plugin_icon = 'DVD.png'
    plugin_version = '1.0.2'
    plugin_author = 'DiscRemux Contributors'
    author_url = ''
    plugin_config_prefix = 'discremux_'
    plugin_order = 30
    auth_level = 1
    plugin_label = '媒体处理'

    def __init__(self):
        """创建实例独立的状态和互斥锁。"""
        super().__init__()
        self._lock = threading.Lock()
        self._config = {}
        self._engine = None
        self._stopping = threading.Event()
        self._status = '等待任务'

    def init_plugin(self, config=None):
        """应用配置，停止旧任务后注册一次性任务。"""
        self.stop_service()
        if not self._lock.acquire(timeout=15):
            raise RuntimeError('旧提取任务尚未停止，请稍后重试')
        try:
            self._config = {**self.get_form()[1], **(config or {})}
            self._stopping.clear()
        finally:
            self._lock.release()
        if self._config.get('run_once'):
            self._config['run_once'] = False
            self.update_config(self._config)
            if not add_plugin_once_job(self.__class__.__name__, 'scan', self.scan, self.plugin_name, delay_seconds=3):
                logger.error('原盘提取：调度器未就绪，请重新勾选运行一次')

    def get_state(self):
        """返回周期扫描是否启用。"""
        return bool(self._config.get('enabled'))

    def get_service(self):
        """向宿主注册周期扫描。"""
        if not self.get_state():
            return []
        return [{'id': 'DiscRemuxScan', 'name': self.plugin_name, 'trigger': 'interval', 'func': self.scan, 'kwargs': {'minutes': max(1, int(self._config.get('interval') or 60))}}]

    @staticmethod
    def get_command():
        """当前不注册消息命令。"""
        return []

    def get_api(self):
        """不公开文件操作 API。"""
        return []

    def get_form(self):
        """提供原生配置界面与默认值。"""
        defaults = dict(enabled=False, run_once=False, source='', output='', iso_kind='bluray', selector='', interval=60, ffmpeg='ffmpeg', ffprobe='ffprobe', timeout=12)
        fields = []
        for key, label in [('enabled', '启用周期扫描'), ('run_once', '保存后立即运行一次')]:
            fields.append({'component': 'VSwitch', 'props': {'model': key, 'label': label}})
        for key, label in [('source', '原盘文件或扫描目录（容器内绝对路径）'), ('output', 'MKV 输出目录（可为扫描目录下的 REMUX，扫描时自动排除）'), ('selector', '蓝光播放列表号/DVD标题号（留空：蓝光主标题、DVD标题1）'), ('interval', '扫描间隔（分钟）'), ('ffmpeg', 'FFmpeg 程序路径'), ('ffprobe', 'FFprobe 程序路径'), ('timeout', '每个子进程最长运行小时数')]:
            fields.append({'component': 'VTextField', 'props': {'model': key, 'label': label}})
        fields.append({'component': 'VSelect', 'props': {'model': 'iso_kind', 'label': 'ISO 类型', 'items': [{'title': '蓝光 / UHD Blu-ray', 'value': 'bluray'}, {'title': 'DVD', 'value': 'dvd'}]}})
        fields.append({'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '仅处理已完整下载、可读取的原盘。音视频字幕均使用 copy；不保留菜单，不转码，不删除源文件，不覆盖已有 MKV。蓝光需 libbluray，DVD 需 dvdvideo。多版本/剧集原盘请指定标题；混合 ISO 请分批配置。输出磁盘需支持硬链接。'}})
        return [{'component': 'VForm', 'content': fields}], defaults

    def get_page(self):
        """显示运行状态和最近处理记录。"""
        records = self.get_data('history') or []
        return [{'component': 'VAlert', 'props': {'type': 'info', 'text': self._status}}, {'component': 'VList', 'content': [{'component': 'VListItem', 'props': {'title': row['source'], 'subtitle': f"{row['time']} | {row['result']}"}} for row in reversed(records)]}]

    def scan(self):
        """串行处理原盘，逐项记载结果并支持停止。"""
        if self._stopping.is_set() or not self._lock.acquire(blocking=False):
            return
        try:
            cfg = dict(self._config)
            cfg['selector'] = str(cfg.get('selector') or '').strip()
            if cfg['selector'] and not cfg['selector'].isascii():
                raise ValueError('标题编号必须为 ASCII 数字')
            if cfg['selector'] and not cfg['selector'].isdecimal():
                raise ValueError('标题编号必须为非负整数')
            if not cfg.get('source') or not cfg.get('output'):
                raise ValueError('请填写输入和输出路径')
            root, output = Path(cfg['source']).expanduser(), Path(cfg['output']).expanduser()
            if not root.is_absolute() or not output.is_absolute():
                raise ValueError('输入和输出必须为绝对路径')
            root, output = root.resolve(), output.resolve()
            if not root.exists():
                raise ValueError('输入路径不存在')
            validate_output(root, output)
            engine = RemuxEngine(cfg['ffmpeg'], cfg['ffprobe'], max(1, float(cfg['timeout'])) * 3600)
            self._engine = engine
            if self._stopping.is_set():
                engine.cancel.set()
                return
            checked = set()
            count = 0
            for source in discover(root, cfg['iso_kind'], exclude=output):
                if self._stopping.is_set():
                    break
                count += 1
                self._status = f'正在提取：{source}'
                try:
                    kind = disc_kind(source, cfg['iso_kind'])
                    if kind not in checked:
                        engine.check(kind)
                        checked.add(kind)
                    relative = Path(source.name) if source == root else source.relative_to(root)
                    name = source.stem if source.is_file() else source.name
                    # 保留来源类型与标题编号，避免同名 ISO/目录、不同标题互相覆盖。
                    suffix = '.iso' if source.is_file() else '.disc'
                    title = '.' + cfg['selector'] if cfg['selector'] else ''
                    target = output / relative.parent / (name + suffix + title + '.mkv')
                    result = engine.remux(source, target, kind, cfg['selector'].strip())
                    logger.info(f'原盘提取 {source}: {result}')
                except Exception as exc:
                    result = f'失败：{exc}'
                    logger.error(f'原盘提取 {source}: {result}')
                history = self.get_data('history') or []
                history.append({'time': datetime.now().isoformat(timespec='seconds'), 'source': str(source), 'result': result[:12000]})
                self.save_data('history', history[-100:])
            self._status = f'扫描结束，发现 {count} 个原盘'
        except Exception as exc:
            self._status = f'扫描失败：{exc}'
            logger.error(self._status)
        finally:
            self._engine = None
            self._lock.release()

    def stop_service(self):
        """取消待执行任务并通知正在运行的子进程退出。"""
        self._stopping.set()
        if self._engine:
            self._engine.cancel.set()
        remove_plugin_once_job(self.__class__.__name__, 'scan')
