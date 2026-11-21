# 环境、安装与依赖

## 环境位置

推荐 Windows + Python 3.12。固定依赖要求 Python 至少 3.12；更新的 Python 版本是否兼容仍取决于依赖，其中 AI 依赖明确排除 Python 3.14.1。Mac/Linux 使用平台无关路径设计，但未经与 Windows 同等实机验证。

分发包根目录的 `install.py` 将 Skill 复制到当前用户主目录下 `.agents/skills/wedding-music-workflow`，独立环境默认在 `.cache/wedding-music-workflow/v3`。这是当前用户路径，不应硬编码他人的用户名或 Windows 盘符。参见 [官方 Skill 安装说明](https://learn.chatgpt.com/docs/build-skills)。

```text
python install.py --dry-run
python install.py
```

首次就需要 AI 环境时，用 `python install.py --separator` 代替基础安装命令。安装后补装 AI 用下方的 bootstrap 命令，不要重复初次安装而覆盖 Skill。

Windows 可使用根目录 `install-windows.cmd` 查找可用的 Python，包括可发现的 Codex 内置运行时。`--skills-dir` 是 Skill 父目录；`--runtime-dir` 是运行环境根目录。`--skip-deps` 只安装文件；`--upgrade` 用于保护旧版后的升级。不要同时安装多个同名副本。

安装成功后，Skill 根目录的 `runtime.json` 由本机生成，记录当前环境路径。此配置、虚拟环境、歌曲、下载会话和模型不应从旧电脑原样复制到新电脑。

## 环境检查与修复

```text
python "<Skill>/scripts/runtime.py" --doctor
python "<Skill>/scripts/runtime.py" --doctor --separator
python "<Skill>/scripts/bootstrap.py" --separator
```

wrapper 会读取配置，选择独立音乐环境，过滤外部 PYTHONPATH，运行目标脚本。AI 分离使用另一独立环境。`bootstrap.py` 未指定路径时会沿用现有 `runtime.json`，配置不存在才使用用户缓存默认路径；可使用 `--runtime-dir` 改路径，`--config` 指定配置文件，`--dry-run` 查看安装计划。

第一次需要网络下载依赖；安装失败时保留已完成部分，读取报错后修复依赖或网络，不能把 `--skip-deps` 当作安装成功。按机器可用 Python 实际路径执行，不能信任指向 Windows Store 的空壳 `python` 命令。

## 组件职责

| 组件 | 职责 |
|---|---|
| 本 Skill / preflight / pipeline | 读取歌单、候选确认、队列、命名、报告、交付保护 |
| musicdl 2.14.2 与本包适配脚本 | 平台元数据及音源适配；不是平台官方插件 |
| imageio-ffmpeg 0.6.0 | 为本机提供可调用的 FFmpeg 二进制 |
| Mutagen 1.48.1 | 读取实际音频格式、时长与码率 |
| NumPy 2.5.4、SoundFile 0.14.0 | 合成音频验证、受校验约束的 PCM 读取 |
| audio-separator 0.47.0、ONNX Runtime、PyTorch 等 | 可选的本地 AI 人声/伴奏分离 |

精确安装项以 `scripts/bootstrap.py` 为准。FFmpeg 不需要独立购买；此处由依赖安装取得。AI 组件首次下载较大，默认不随基础安装启用。模型也另行下载，不随 ZIP 分发。

## 上游许可与使用范围

分发包只包含工作流脚本、说明和合成音频测试代码，不捆绑上游程序包、FFmpeg 二进制、AI 模型或真实歌曲。安装时由包管理器取得上游发行物，保留其各自许可。

`musicdl 2.14.2` 的发行元数据标注 **PolyForm-Noncommercial-1.0.0**，即非商业许可。商业用途需另行确认适用范围或取得上游授权；本包不将它重新许可为无限制商用软件。

上游链接：

- [musicdl 项目与许可证](https://github.com/CharlesPikachu/musicdl)
- [FFmpeg 许可说明](https://ffmpeg.org/legal.html)
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)
- [Mutagen](https://github.com/quodlibet/mutagen)
- [NumPy](https://github.com/numpy/numpy)
- [SoundFile](https://github.com/bastibe/python-soundfile)
- [audio-separator](https://github.com/nomadkaraoke/python-audio-separator)

歌曲平台权限和作品使用范围不会因为安装本 Skill 而改变。当前流程不携带 Cookie、账号令牌或订阅权限；不从浏览器提取账号数据，不解密 DRM。
