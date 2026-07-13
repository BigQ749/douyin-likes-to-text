# 抖音视频转文字工具（Windows 本机桌面版）

一个以**本机处理和可选择性**为核心的 Windows 桌面工具：先从自己的抖音「喜欢」或「收藏」中读取视频清单，由你勾选需要处理的视频，再进行临时下载与本机语音转写。

> 仅处理你有权访问和处理的抖音内容，并请遵守抖音及相关服务的规则。

## 功能

- **抓取喜欢视频**：按你填写的数量读取标题、作者、简介、链接；不在这一步下载视频。
- **抓取收藏视频**：读取收藏夹内的视频，先展示清单再选择。
- **单条分享链接**：可直接粘贴完整的抖音分享文案；工具会自动从文字中识别有效链接。
- **勾选式处理**：支持筛选、全选、取消全选、反选和清空，只有勾选的视频会进入下载与转写流程。
- **本机临时转写**：视频仅临时保存到本机，完成转写后会自动清理原视频和下载元数据。
- **TXT 输出**：结果写入 `output/待Codex总结.txt`，包含标题、视频链接和转写文字，便于继续交给 Codex/ChatGPT 进行人工辅助总结。
- **避免重复处理**：已成功转写且有有效文本的视频会自动跳过。
- **进度反馈**：桌面窗口显示下载与转写的阶段和当前条目进度。
- **无黑色终端窗口**：通过 `launch-desktop-app.vbs` 启动桌面窗口。

## 隐私与数据

- 不需要 OpenAI API Key，也不会把视频、音频、Cookie、账号登录信息或转写结果上传给本项目的服务端。
- 登录 Cookie、模型、原视频、转写结果、SQLite 状态库和运行时文件都只保存在本机，并被 `.gitignore` 明确排除。
- 首次使用时，语音模型与浏览器组件会下载到本机；这是主要的磁盘占用来源。视频文件不会长期累积。
- 处理完成后仍建议你确认 `output/` 中的 TXT 是否需要长期保留或自行备份。

## 环境要求

- Windows 10/11
- [Git for Windows](https://git-scm.com/download/win)
- [uv](https://docs.astral.sh/uv/)
- 可访问抖音的网络环境

## 首次安装

在 PowerShell 中进入项目根目录，运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

该脚本会：

1. 安装本桌面工具的 Python 依赖；
2. 下载并安装开源的 `jiji262/douyin-downloader` 依赖到本机 `.deps/`；
3. 安装登录所需的浏览器组件。

依赖目录 `.deps/` 不会提交到 Git，也不会包含你的登录 Cookie。

## 使用方法

1. 双击 `launch-desktop-app.vbs`（可自行创建桌面快捷方式）。
2. 第一次使用或登录失效时，点击窗口中的 **重新登录抖音**，在弹出的浏览器中完成登录。
3. 在数量栏填写想先读取的数量，例如 `50`、`100` 或 `1000`。
4. 点击 **抓取喜欢视频** 或 **抓取收藏视频**；也可以粘贴单条抖音分享文案。
5. 在列表中勾选要转写的条目。
6. 点击 **将已选视频转为文字**，在窗口中观察下载/转写进度。
7. 完成后打开 `output/待Codex总结.txt`。如需内容概述，可把该 TXT 提供给 Codex/ChatGPT 继续总结。

## 文件结构

```text
.
├─ desktop_app.py              # Tkinter 桌面应用
├─ process_likes.py            # 本机 faster-whisper 转写与 TXT 输出
├─ app_paths.py                # 本地依赖路径发现
├─ launch-desktop-app.vbs      # 隐藏终端的 Windows 启动入口
├─ desktop_app_launcher.pyw    # GUI Python 启动器
├─ scripts/
│  └─ bootstrap.ps1            # 新机器首次安装
├─ tests/                      # 自动化测试
├─ config.example.yml          # 本机转写配置样例
└─ douyin-downloader-config.example.yml
```

下列目录/文件是**本机私有数据**，不会上传到 GitHub：

```text
.deps/  .venv/  config/  config.yml  config.local.yml
input/  output/  models/  runtime/（除说明文件）
*.mp4  *.mp3  *.wav  *.sqlite3  *cookie*.json  *token*.json
```

## 常见问题

### 抓取时提示 Cookie 无效或下载 0 条

在应用内点击 **重新登录抖音**，完成登录后再重试。抖音登录状态可能会过期；请勿把本机 `cookies.json` 上传或发给任何人。

### 提示缺少 `aiohttp` 或浏览器组件

说明首次依赖安装没有完成。关闭应用后重新运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

### 为什么 TXT 中某条没有正常语音内容？

部分视频没有可识别的人声、背景声过强，或下载源没有提供可转写的音轨。工具会保留明确的说明文字，不会把空白转写误标记为处理完成。

### 如何继续让 AI 总结内容？

本工具负责在本机生成「标题 + 链接 + 转写文字」的 TXT。你可在 Codex/ChatGPT 对话中上传或粘贴该 TXT，并说明希望得到的总结格式（例如按主题、重点观点、待办事项或表格）。

## 第三方依赖

- 下载能力依赖开源项目 [`jiji262/douyin-downloader`](https://github.com/jiji262/douyin-downloader)（MIT License）。
- 界面主题使用 `sv-ttk`（MIT License）。
- 本地语音转写使用 `faster-whisper`。

本仓库不直接提交上述下载器的源代码；首次安装脚本会从其上游仓库下载依赖。
