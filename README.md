# MoviePilot v3 原盘无损提取

使用 FFmpeg 将**可正常读取的蓝光/UHD Blu-ray、DVD 原盘目录和 ISO**封装为 MKV。视频、全部音轨和字幕使用 `-c copy`，不重新编码；复制章节和元数据。仅复制导航器暴露的所选标题轨道，蓝光菜单、BD-J、交互功能和附件不属于输出内容。

## 安装

1. 在 MoviePilot v3 的第三方插件仓库设置添加 `https://github.com/iumuu/MoviePilot-Plugins-DiscRemux`。
2. 刷新插件市场，安装「原盘无损提取」（插件 ID：`DiscRemux`）。
3. 配置容器内输入、输出绝对路径，以及 FFmpeg / FFprobe 程序路径。
4. 勾选「保存后立即运行一次」进行首次处理；需要自动扫描时开启周期扫描。

采用 `package.v3.json` + `plugins.v3/discremux/`，原生 Vuetify 配置界面；使用当前 MoviePilot v3 SDK（包括 `app.sdk.scheduler.add_plugin_once_job`）。旧版 v3 如果缺少该接口，请更新宿主。没有 Python 第三方运行依赖。

## FFmpeg 依赖和 ISO 支持

FFmpeg 与 FFprobe 必须来自具备相同导航功能的构建：

| 来源 | 必需能力 | 默认标题 |
| --- | --- | --- |
| 蓝光/UHD 目录（包含 BDMV）或 ISO | `bluray` 协议，编译启用 libbluray；ISO 读取还取决于 libbluray 的 UDF 支持 | libbluray 主标题 |
| DVD 目录（包含 VIDEO_TS）或 ISO | `dvdvideo` demuxer，启用 libdvdnav 和 libdvdread | 标题 1 |

检查命令：

```sh
ffmpeg -protocols
ffprobe -protocols
ffmpeg -demuxers
ffprobe -demuxers
```

只装了普通 FFmpeg 不代表具备上述能力。插件启动处理前检查能力，无法读取的 ISO 会显示 FFmpeg 原始错误。**ISO 直接通过导航库读取，无需挂载，也无需容器 privileged 权限。插件不提供解密功能**；加密、损坏或导航混淆原盘可能无法读取。UHD 的 HDR/杜比视界等是否完整保留取决于 FFmpeg、输入轨道及封装支持，当前未完成这些格式的样本验证。

蓝光按播放列表读取全部分段，不拼接最大 M2TS；可填写播放列表号，例如 `800` 对应 `00800.mpls`。DVD 可填写标题号，例如 `2`。多版本或剧集原盘需要分次选择标题，不会自动识别所有集数。目录类型自动识别；ISO 类型使用配置项，混合蓝光/DVD ISO 请分批处理。

## 路径示例

```yaml
# 合并到现有 MoviePilot compose 的 volumes 中
volumes:
  - /media/discs:/discs:ro
  - /media/remux:/remux
```

输入 `/discs`，输出 `/remux`。输入也可以是单个 ISO 或包含 BDMV/VIDEO_TS 的原盘根目录。不要直接填写 BDMV/STREAM。输出可以位于扫描目录内，例如输入 `/media/downloads`、输出 `/media/downloads/REMUX`；扫描时自动排除整个输出目录。输出不能与输入相同，也不能是输入的父目录。请仅扫描已下载完成且不再写入的原盘，插件不连接下载器判断下载状态。

输出保留相对目录，并带来源和显式标题后缀：`Film.iso.mkv`、`Film.disc.mkv`、`Film.iso.800.mkv`。现有输出直接跳过；若更换了同名源文件，需要自行移走旧输出才能重做。

## 任务与数据保护

- 串行处理，重复触发不会并发执行；停止/重载时取消当前 FFmpeg 子进程。
- 先写同目录临时 MKV，成功后检查视频/音频/字幕的编码与数量、时长差异、章节数量，再原子创建最终文件。检查不等于逐帧解码验证。
- 输出文件系统必须支持硬链接（常见 ext4、Btrfs、NTFS 支持；部分网盘/FUSE/SMB 挂载不支持）。不支持时任务失败，不会退化为覆盖写入。
- FFmpeg 不支持的字幕或音频会使任务失败，不会悄悄转码或丢弃轨道。
- 正常失败/取消清理本次临时文件和锁；宿主崩溃或断电可能留下 `.partial.mkv` / `.mkv.lock`。确认没有活跃任务后手工删除对应残留再试。
- 源文件只读，不删除、不改名；预留足够输出空间。最近 100 条任务结果显示在详情页。

## 验证与限制

```sh
python -m compileall -q plugins.v3
python -m unittest discover -s tests -v
```

测试覆盖目录发现、导航参数、全轨复制命令、重复输出保护、锁竞争、校验失败清理、进程错误/超时及取消。本地 Python 3.13 的 17 项测试已通过；当前 GitHub 授权未包含 workflow 权限，仓库暂未启用 Actions。

当前属于首版：已对照 MoviePilot v3 官方 SDK 编写，尚未在真实 MoviePilot 实例中安装，也未使用真实蓝光/DVD/ISO 样本做端到端验证。首次使用请先用一部已知标题的原盘核对音轨、字幕、章节与播放情况。

许可证：MIT。
