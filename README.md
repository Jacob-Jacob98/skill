# 一生主持人 · 客户技能安装

在 Windows 上打开 **PowerShell**，按你使用的客户端复制一条命令，粘贴后按回车。需要能访问 GitHub，并已安装对应的客户端。

## Gemini CLI

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing 'https://raw.githubusercontent.com/Jacob-Jacob98/skill/main/install.ps1'))) gemini
```

打开 Gemini CLI，输入 `/skills reload`，再输入 `/skills list`。列表里会有 **婚礼谈单军师** 和 **婚礼应急救场宝典**。可以试试“婚礼现场音响突然断了，给我两版能马上说的救场词”。

## OpenAI Codex（本地 GPT 工作流）

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing 'https://raw.githubusercontent.com/Jacob-Jacob98/skill/main/install.ps1'))) gpt
```

打开新的 Codex 对话，输入 `$` 选择技能。**image-prompt-replication** 可以根据参考图反推提示词；**wedding-music-workflow** 用于婚礼选曲、剪辑与交付。

婚礼歌单的基础音乐环境推荐 **Python 3.12**。安装脚本会查找可用的 Python，联网下载依赖，并用一段测试音频验证 MP3 编码。如果提示缺少 Python，请安装 Windows 版 Python 3.12 后重跑同一条命令。仅安装技能文件可在命令末尾加 `-SkillsOnly`。AI 去人声是可选功能，使用前按[伴奏说明](gpt/wedding-music-workflow/references/separation.md)准备模型与依赖。商业使用音乐下载组件前，请查看[原工作流的依赖许可说明](gpt/wedding-music-workflow/references/dependencies.md)。

## 文件装在哪里

| 客户端 | 当前 Windows 用户的技能目录 |
| --- | --- |
| Gemini CLI | `%USERPROFILE%\.gemini\skills` |
| OpenAI Codex | `%USERPROFILE%\.agents\skills` |

同一命令可以重复运行。已安装且内容一致的技能会复用；如果同名技能内容有差异，脚本会显示对应路径，方便你先备份并移走旧版。Gemini 网页版和 ChatGPT 网页版使用各自的在线配置，此处的命令面向本地客户端。
