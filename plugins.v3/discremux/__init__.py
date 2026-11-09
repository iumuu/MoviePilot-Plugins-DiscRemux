"""MoviePilot v3 原盘无损提取插件。"""
import threading
import time
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

from app.sdk.plugin import _PluginBase
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .engine import RemuxEngine, discover, disc_kind, validate_output, output_path
from .runtime import select_tools


class DiscRemux(_PluginBase):
    """定时扫描原盘并无损封装 MKV。"""
    plugin_name = '原盘无损提取'
    plugin_desc = '使用 FFmpeg 将蓝光/DVD 目录及 ISO 无损封装为 MKV。'
    plugin_icon = 'DVD.png'
    plugin_version = '1.5.0'
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
        self._last_progress_log = 0
        self._progress_source = ''
        self._pending = False

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
        self._config.pop('redo_once', None)
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
        """提供登录鉴权的记录操作，不接受任意文件路径。"""
        return [{'path': '/record_action', 'endpoint': self.record_action, 'methods': ['POST'], 'auth': 'bear', 'summary': '清除记录或单条重新提取'}]

    def get_form(self):
        """提供原生配置界面与默认值。"""
        defaults = dict(enabled=False, run_once=False, source='', output='', iso_kind='bluray', selector='', interval=60, ffmpeg='ffmpeg', ffprobe='ffprobe', timeout=12, prefer_system=True, auto_install=False)
        fields = []
        for key, label in [('enabled', '启用周期扫描'), ('run_once', '保存后立即运行一次')]:
            fields.append({'component': 'VSwitch', 'props': {'model': key, 'label': label}})
        for key, label in [('prefer_system', '优先使用系统版 FFmpeg（/usr/bin，忽略下方自定义路径）'), ('auto_install', '缺失时自动安装系统版 FFmpeg（Debian/Ubuntu、root、需联网）')]:
            fields.append({'component': 'VSwitch', 'props': {'model': key, 'label': label}})
        fields.append({'component': 'VAlert', 'props': {'type': 'info', 'text': '自动安装仅在优先系统版开启且工具缺失时执行 apt update/install。容器重建后会在下次扫描自动补装；已有系统工具不会重复安装。安装需要可写的系统目录，可能持续数分钟。'}})
        for key, label in [('source', '原盘文件或扫描目录（容器内绝对路径）'), ('output', 'MKV 输出目录（可为扫描目录下的 REMUX，扫描时自动排除）'), ('selector', '蓝光播放列表号/DVD标题号（留空：蓝光主标题、DVD标题1）'), ('interval', '扫描间隔（分钟）'), ('ffmpeg', 'FFmpeg 程序路径'), ('ffprobe', 'FFprobe 程序路径'), ('timeout', '每个子进程最长运行小时数')]:
            fields.append({'component': 'VTextField', 'props': {'model': key, 'label': label}})
        fields.append({'component': 'VSelect', 'props': {'model': 'iso_kind', 'label': 'ISO 类型', 'items': [{'title': '蓝光 / UHD Blu-ray', 'value': 'bluray'}, {'title': 'DVD', 'value': 'dvd'}]}})
        fields.append({'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '仅处理已完整下载、可读取的原盘。音视频字幕均使用 copy；不保留菜单，不转码，不删除源文件，不覆盖已有 MKV。蓝光需 libbluray，DVD 需 dvdvideo。多版本/剧集原盘请指定标题；混合 ISO 请分批配置。输出磁盘需支持硬链接。'}})
        return [{'component': 'VForm', 'content': fields}], defaults

    @staticmethod
    def _row_id(row):
        """生成源路径和标题对应的稳定记录 ID。"""
        selector = str(row.get('selector') or '').strip()
        if selector.isdecimal():
            selector = str(int(selector))
        return hashlib.sha256(json.dumps([row.get('source'), selector], ensure_ascii=False).encode()).hexdigest()

    def _completed_rows(self):
        """读取全部持久成功记录，包括超出历史日志上限的记录。"""
        return [(item.key, item.value) for item in (self.get_data() or []) if item.key.startswith('completed_')]

    def _record_rows(self):
        """按原盘及标题合并重复日志，加入持久成功记录。"""
        rows = {self._row_id(row): dict(row) for row in self.get_data('discovered') or []}
        for _, row in self._completed_rows():
            rows[self._row_id(row)] = {**row, 'result': '完成（持久记录）'}
        for row in self.get_data('history') or []:
            rows[self._row_id(row)] = {**rows.get(self._row_id(row), {}), **row}
        return rows

    def get_page(self):
        """在数据页顶部及每行右侧提供记录操作。"""
        busy = self._lock.locked() or self._pending
        def button(text, action, record_id=''):
            """创建原生页面 POST 操作按钮。"""
            return {'component': 'VBtn', 'text': text, 'props': {'size': 'small', 'variant': 'tonal', 'color': 'error' if action.startswith('clear') else 'primary', 'disabled': busy}, 'events': {'click': {'api': 'plugin/DiscRemux/record_action', 'method': 'POST', 'params': {'action': action, 'record_id': record_id}}}}
        content = []
        for record_id, row in sorted(self._record_rows().items(), key=lambda item: item[1].get('time') or '', reverse=True):
            content.append({'component': 'VRow', 'props': {'align': 'center', 'class': 'mb-3'}, 'content': [
                {'component': 'VCol', 'props': {'cols': 12, 'md': 8}, 'content': [
                    {'component': 'div', 'text': row['source'], 'props': {'style': 'overflow-wrap:anywhere'}},
                    {'component': 'div', 'text': f"{row.get('time', '')} | 标题：{row.get('selector') or '默认'} | {row.get('result', '')}", 'props': {'style': 'overflow-wrap:anywhere'}}]},
                {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'd-flex ga-2 justify-end flex-wrap'}, 'content': [button('提取', 'extract', record_id), button('清除单条记录', 'clear_one', record_id), button('重新提取', 'redo', record_id)]}]})
        return [{'component': 'VAlert', 'props': {'type': 'info', 'text': f'版本 {self.plugin_version} | {self._status}'}},
                {'component': 'div', 'props': {'class': 'my-3 d-flex ga-2'}, 'content': [button('扫描原盘（不提取）', 'discover'), button('刷新列表', 'refresh'), button('清除记录', 'clear_all')]},
                {'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'text': '清除记录会同时清除成功标记，不删除文件；输出已删除时，下次扫描可能重新提取。重新提取仅处理所选原盘，并保留已有输出。'}},
                {'component': 'VContainer', 'content': content}]

    def record_action(self, payload: dict):
        """按服务端记录执行操作，拒绝运行中修改或重复排队。"""
        if not self._lock.acquire(blocking=False):
            return {'success': False, 'data': None, 'message': '任务正在运行，请结束后再操作'}
        try:
            if self._pending or self._stopping.is_set():
                return {'success': False, 'data': None, 'message': '任务等待执行或插件正在停止'}
            action = payload.get('action')
            if action == 'refresh':
                return {'success': True, 'data': None, 'message': '列表已刷新'}
            if action == 'discover':
                self._pending = True
                try:
                    if not add_plugin_once_job(self.__class__.__name__, 'discover', self.discover_sources, '扫描原盘（不提取）', delay_seconds=1):
                        raise RuntimeError('宿主调度器未就绪')
                except Exception as exc:
                    self._pending = False
                    self._status = f'扫描排队失败：{exc}'
                    return {'success': False, 'data': None, 'message': self._status}
                self._status = '原盘扫描已排队；扫描完成后点击刷新列表'
                return {'success': True, 'data': None, 'message': self._status}
            record_id = payload.get('record_id', '')
            rows = self._record_rows()
            if action not in ('clear_all', 'clear_one', 'redo', 'extract'):
                return {'success': False, 'data': None, 'message': '无效操作'}
            if action != 'clear_all' and record_id not in rows:
                self._status = '记录已不存在，请刷新页面'
                return {'success': False, 'data': None, 'message': self._status}
            if action in ('redo', 'extract'):
                row = rows[record_id]
                source = Path(row['source'])
                if not source.is_absolute() or not disc_kind(source, row.get('kind') or self._config.get('iso_kind', 'bluray')):
                    self._status = '无法重新提取：原盘不存在或不是有效原盘根目录'
                    return {'success': False, 'data': None, 'message': self._status}
                self._pending = True
                try:
                    accepted = add_plugin_once_job(self.__class__.__name__, 'redo-record', self.scan, '单条原盘提取', delay_seconds=1, func_kwargs={'redo': action == 'redo', 'record': dict(row)})
                    if not accepted:
                        raise RuntimeError('宿主调度器未就绪')
                except Exception as exc:
                    self._pending = False
                    self._status = f'排队失败：{exc}'
                    return {'success': False, 'data': None, 'message': self._status}
                self._status = f'已排队，仅处理所选原盘：{source}'
            else:
                history = self.get_data('history') or []
                self.save_data('history', [] if action == 'clear_all' else [row for row in history if self._row_id(row) != record_id])
                discovered = self.get_data('discovered') or []
                self.save_data('discovered', [] if action == 'clear_all' else [row for row in discovered if self._row_id(row) != record_id])
                for key, row in self._completed_rows():
                    if action == 'clear_all' or self._row_id(row) == record_id:
                        self.del_data(key)
                self._status = '已清除全部记录和完成标记' if action == 'clear_all' else '已清除该原盘标题的记录和完成标记'
            return {'success': True, 'data': None, 'message': self._status}
        finally:
            self._lock.release()

    def discover_sources(self):
        """只发现原盘并持久保存候选列表，不安装工具或运行提取。"""
        with self._lock:
            try:
                if self._stopping.is_set():
                    return
                cfg = dict(self._config)
                if not cfg.get('source') or not cfg.get('output'):
                    raise ValueError('请先配置输入和输出目录')
                root, output = Path(cfg['source']).expanduser(), Path(cfg['output']).expanduser()
                if not root.is_absolute() or not output.is_absolute():
                    raise ValueError('输入输出必须为绝对路径')
                root, output = root.resolve(), output.resolve()
                if not root.exists():
                    raise ValueError('输入路径不存在')
                validate_output(root, output)
                rows = []
                for source in discover(root, cfg.get('iso_kind', 'bluray'), exclude=output):
                    if self._stopping.is_set():
                        return
                    rows.append({'source': str(source), 'kind': disc_kind(source, cfg.get('iso_kind', 'bluray')), 'selector': str(cfg.get('selector') or '').strip(), 'time': datetime.now().isoformat(timespec='seconds'), 'result': '已发现，等待手动提取'})
                    self._status = f'扫描中，发现 {len(rows)} 个原盘'
                self.save_data('discovered', rows)
                self._status = f'扫描完成，发现 {len(rows)} 个原盘；点击刷新列表后可逐条提取'
                logger.info(self._status)
            except Exception as exc:
                self._status = f'原盘扫描失败：{exc}'
                logger.error(self._status)
            finally:
                self._pending = False

    def scan(self, redo=False, record=None):
        """串行处理原盘，逐项记载结果并支持停止。"""
        if self._stopping.is_set() or (self._pending and record is None) or not self._lock.acquire(blocking=False):
            return
        try:
            cfg = dict(self._config)
            if record is not None:
                cfg.update(source=record['source'], selector=record.get('selector', ''), iso_kind=record.get('kind') or cfg.get('iso_kind', 'bluray'))
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
            if redo and not disc_kind(root, cfg['iso_kind']):
                raise ValueError('重新提取请将输入设置为单个 ISO 或含 BDMV/VIDEO_TS 的原盘根目录，不能使用批量扫描目录')
            engine = None
            checked = set()
            count = 0
            for source in discover(root, cfg['iso_kind'], exclude=output):
                if self._stopping.is_set():
                    break
                count += 1
                self._status = f'正在提取：{source}'
                self._progress_source = source.name
                self._last_progress_log = 0
                try:
                    kind = disc_kind(source, cfg['iso_kind'])
                    selector = str(int(cfg['selector'])) if cfg['selector'] else ''
                    identity = json.dumps([str(source.resolve()), kind, selector], ensure_ascii=False)
                    record_key = 'completed_' + hashlib.sha256(identity.encode()).hexdigest()
                    completed = self.get_data(record_key)
                    if not completed:
                        for old in self.get_data('history') or []:
                            old_selector = str(old.get('selector') or '').strip()
                            if old_selector.isdecimal():
                                old_selector = str(int(old_selector))
                            if old.get('source') == str(source) and old.get('result') == '完成' and old_selector == selector:
                                completed = {'source': str(source), 'kind': kind, 'selector': selector, 'time': old.get('time'), 'migrated': True}
                                self.save_data(record_key, completed)
                                break
                    if completed and not redo:
                        logger.info(f'原盘提取 {source}: 已跳过：持久成功记录存在（无需保留输出文件）')
                        continue
                    if engine is None:
                        ffmpeg, ffprobe = select_tools(cfg, self._stopping, self._report_runtime)
                        engine = RemuxEngine(ffmpeg, ffprobe, max(1, float(cfg['timeout'])) * 3600, report=self._report_progress)
                        self._engine = engine
                    if self._stopping.is_set():
                        engine.cancel.set()
                        break
                    if kind not in checked:
                        engine.check(kind)
                        checked.add(kind)
                    target = output_path(source, root, output, cfg['selector'])
                    if redo and target.exists():
                        target = target.with_name(target.stem + '.redo-' + uuid.uuid4().hex[:12] + '.mkv')
                    result = engine.remux(source, target, kind, cfg['selector'].strip())
                    if result == '完成':
                        self.save_data(record_key, {'source': str(source), 'kind': kind, 'selector': selector, 'output': str(target), 'time': datetime.now().isoformat(timespec='seconds')})
                    logger.info(f'原盘提取 {source}: {result}')
                except Exception as exc:
                    result = f'失败：{exc}'
                    logger.error(f'原盘提取 {source}: {result}')
                history = self.get_data('history') or []
                history.append({'time': datetime.now().isoformat(timespec='seconds'), 'source': str(source), 'kind': kind, 'selector': cfg['selector'], 'result': result[:12000]})
                self.save_data('history', history[-100:])
            self._status = f'扫描结束，发现 {count} 个原盘'
            logger.info(self._status)
        except Exception as exc:
            self._status = f'扫描失败：{exc}'
            logger.error(self._status)
        finally:
            self._engine = None
            if record is not None:
                self._pending = False
            self._lock.release()

    def _report_runtime(self, message):
        """在详情和宿主日志中显示依赖准备状态。"""
        self._status = message
        logger.info(message)

    def _report_progress(self, message):
        """更新详情状态，进度日志每十秒输出，阶段切换立即输出。"""
        self._status = f'{self._progress_source} | {message}'
        periodic = message.startswith('提取中') or '已运行' in message
        if not periodic or time.monotonic() - self._last_progress_log >= 10:
            logger.info(self._status)
            self._last_progress_log = time.monotonic()

    def get_dashboard_meta(self):
        """提供可自动刷新的实时进度卡片。"""
        return [{'key': 'progress', 'name': '原盘提取进度'}]

    def get_dashboard(self, key, **kwargs):
        """每三秒刷新任务阶段与当前进度。"""
        return {'cols': 12, 'md': 6}, {'refresh': 3, 'title': self.plugin_name}, [{'component': 'VAlert', 'props': {'type': 'info', 'text': self._status}}]

    def stop_service(self):
        """取消待执行任务并通知正在运行的子进程退出。"""
        self._stopping.set()
        if self._engine:
            self._engine.cancel.set()
        remove_plugin_once_job(self.__class__.__name__, 'scan')
        remove_plugin_once_job(self.__class__.__name__, 'redo-record')
        remove_plugin_once_job(self.__class__.__name__, 'discover')
        self._pending = False
