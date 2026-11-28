<div align="center">

<img src="logo.png" width="120" alt="音声下载 logo">

# 音声下载（KikoFlu）

**astrbot_plugin_kikoeru_dl**

<img src="https://count.getloli.com/@astrbot-plugin-kikoeru-dl?name=astrbot-plugin-kikoeru-dl&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt="visitors">

在聊天里检索 KikoFlu 音声库的中文字幕作品，按音轨发送内嵌字幕的 MP4 / MP3 / WAV

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-1b5e4a?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-v1.6.2-b93928?style=flat-square)](#)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Backend](https://img.shields.io/badge/backend-kikoeru--express_API-6b4fbb?style=flat-square)](https://github.com/kikoeru-project/kikoeru-express)
[![FFmpeg](https://img.shields.io/badge/requires-FFmpeg-007808?style=flat-square&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)

[功能](#-功能一览) · [快速开始](#-快速开始) · [聊天用法](#-聊天用法) · [AI 工具](#-ai-工具) · [配置](#%EF%B8%8F-配置项) · [常见问题](#-常见问题) · [开发维护](#%EF%B8%8F-开发与维护)

</div>

---

## ✨ 功能一览

| | 功能 | 说明 |
| :---: | --- | --- |
| 🔍 | **作品检索** | 按名称、社团、标签或 RJ 号搜索，普通关键词只保留中文字幕作品 |
| 🎴 | **作品卡片** | 每部作品一张图片卡，同一批合并发送；自动合并同名音轨的 MP3 / WAV / 音效版本 |
| 🔀 | **换一批** | 顺序或随机续抓，自动跳过本轮已发过的作品 |
| 📋 | **我的列表** | 读取 KikoFlu 在线的想听、在听、听过、重听、搁置列表 |
| 🏷️ | **标记作品** | 直接在聊天里把作品标为想听 / 在听 / 听过等，或取消标记 |
| 🎧 | **音轨下载** | 单条或批量发送音轨，支持 MP4、MP3、WAV，字幕写进文件里 |
| 🔇 | **音效版本** | 作品含无音效版本时会在卡片上标注，下载时可以指定有 / 无音效 |
| 💾 | **整部保存** | 把整部作品连同原文件和独立字幕保存到本地目录 |
| 🚫 | **标签屏蔽** | 命中排除标签的作品不会出现在列表里，直查和下载也会拦截 |
| ⚡ | **快捷回复** | 发 RJ 号、「换一批」「第 2 部第 3 号」由插件直接处理，不经过 LLM |

## 🚀 快速开始

### 1. 准备环境

| 依赖 | 用途 |
| --- | --- |
| KikoFlu 后端（kikoeru-express 兼容 API） | 音声数据来源，需要后端地址和登录 token |
| [FFmpeg](https://ffmpeg.org/) | 生成 MP4、在不同格式之间转换 |
| `mutagen`、`Pillow` | 写入音频字幕标签、绘制作品卡片 |

FFmpeg 需要在系统 `PATH` 里，或者装在 `C:\ffmpeg\bin`、`D:\ffmpeg\bin`、`C:\Program Files\ffmpeg\bin` 这类常见位置。

### 2. 安装插件

把源码放到 `data/plugins/astrbot_plugin_kikoeru_dl/`，确保 `metadata.yaml` 和 `main.py` 直接位于该目录下，然后重载插件。也可以打包成 ZIP 在 AstrBot「插件」页上传安装。

### 3. 连接后端

二选一即可：

- **自动读取（推荐）：** 本机装了 KikoFlu 客户端并已登录时，配置全部留空，插件会自动读取客户端里的后端地址、token 和下载目录
- **手动填写：** 在插件配置页填写「KikoFlu 后端地址」和「KikoFlu 登录 token」，例如 `https://api.asmr-200.com`

> [!TIP]
> 「KikoFlu 后端代理地址」默认留空，插件会直接连接后端。仅在 AstrBot 所在环境需要代理时填写，例如 `http://proxy:8080`。

### 4. 开始使用

给当前会话启用插件和对应的 LLM 工具，然后直接用自然语言跟 Bot 说话就行。

## 💬 聊天用法

| 想做什么 | 可以这样说 |
| --- | --- |
| 搜索作品 | 搜一下治愈系的音声 · 找这个社团的作品 |
| 直查作品 | RJ01234567 |
| 换一批 | 换一批 · 随机换一批 |
| 我的列表 | 看看我的想听列表 · 随机抽 5 部我听过的 |
| 标记作品 | 把第 2 部标成想听 · 第 1、3 部标为听过 · 取消标记第 4 部 |
| 查看音轨 | 看看第 2 部的音轨 |
| 下载音轨 | 第 2 条用 MP3 发我 · 把第 1、3 条下载成带字幕的 WAV · 下载 2-6 条 |
| 快捷下载 | 第 2 部第 3 号，发无音效版 WAV |
| 整部保存 | 把第 1 部整部下载到本地 |

> [!NOTE]
> **快捷回复**需要 @Bot 或使用唤醒词。RJ 号随时可用；「换一批」和「第 X 部第 Y 号」只在收到作品卡后的 **10 分钟内**、由同一个人发送时生效。

<details>
<summary><b>📋 下载格式细节（点击展开）</b></summary>

<br>

| 格式 | 发送方式 | 字幕 |
| --- | --- | --- |
| **MP4** | 视频，封面 + 音频 | 带字幕 |
| **MP3** | 文件附件 | 写入同步歌词（ID3 SYLT） |
| **WAV** | 文件附件 | 写入同步歌词；原文件没有时间标记时，额外写入 RIFF 字幕标记 |

- 没有指定格式时，使用配置里的「默认下载格式」（初始为 MP4），聊天里明确说的格式优先
- 和原音频同格式时直接保留原文件，不同格式才用 FFmpeg 转换
- 整部保存不做转换，保留原文件和独立字幕文件

</details>

## 🤖 AI 工具

| 工具 | 用途 |
| --- | --- |
| `kikoeru_search` | 按名称、社团、标签或 RJ 号搜索，发送作品卡；支持顺序 / 随机续抓 |
| `kikoeru_list` | 读取想听、在听、听过、重听、搁置或全部列表，发送作品卡 |
| `kikoeru_mark` | 按卡片编号、RJ 号或作品链接设置标记状态，或取消标记；多部用逗号分隔 |
| `kikoeru_download` | 查看音轨列表，单条或批量发送音轨，或把整部保存到本地 |

## ⚙️ 配置项

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `kikoeru_url` | 留空 | 后端地址，留空自动读取 KikoFlu 客户端配置 |
| `kikoeru_token` | 留空 | 登录 token，留空自动读取 KikoFlu 客户端配置 |
| `kikoeru_proxy` | 留空 | 留空直连。填写后使用指定代理。 |
| `kikoeru_default_format` | `mp4` | 默认下载格式：`mp4` / `mp3` / `wav` |
| `kikoeru_excluded_tags` | 空 | 排除标签，每项一个完整标签名，匹配原名或后端译名 |
| `kikoeru_download_dir` | 留空 | 整部保存目录，留空使用 KikoFlu 的下载目录（未设置则为 `Documents/downloads`） |

## ❓ 常见问题

<details>
<summary><b>提示「未找到 ffmpeg」？</b></summary>

<br>

安装 FFmpeg，并把它的 `bin` 目录加入系统 `PATH`，或者放到 `C:\ffmpeg\bin` 之类的常见位置，然后重启 AstrBot。

</details>

<details>
<summary><b>请求后端经常超时？</b></summary>

<br>

如果直连后端经常超时，且 AstrBot 所在环境需要通过代理访问，可在「KikoFlu 后端代理地址」填写可用的代理。启用代理后，只有 GET 请求遇到连接错误或超时时会重试一次直连。

</details>

<details>
<summary><b>不装 KikoFlu 客户端能用吗？</b></summary>

<br>

可以。在配置页手动填写后端地址和 token 即可。自动读取只是省去手动填写这一步（目前读取的是 Windows 下 `%APPDATA%\com.meteor\KikoFlu\shared_preferences.json`）。

</details>

<details>
<summary><b>说「换一批」没有反应？</b></summary>

<br>

快捷回复只在收到作品卡后 10 分钟内、由同一个人发送时生效，并且需要 @Bot 或唤醒词。超过时间后正常和 Bot 对话即可，LLM 会继续沿用上次的关键词。

</details>

<details>
<summary><b>为什么搜不到某些作品？</b></summary>

<br>

普通关键词搜索只保留带中文字幕的作品；另外命中「排除标签」的作品也会被过滤掉。想查某部具体作品时，直接发 RJ 号或完整标题。

</details>

## 🛠️ 开发与维护

<details>
<summary><b>后端接口与文件结构（点击展开）</b></summary>

<br>

**文件**

| 文件 | 内容 |
| --- | --- |
| `main.py` | 主逻辑、LLM 工具、快捷回复 |
| `work_cards.py` | 作品卡片绘制 |
| `audio_subtitles.py` | MP3 / WAV 字幕写入 |
| `_conf_schema.json` | 配置页定义 |
| `metadata.yaml` | 插件信息 |

**依赖的 kikoeru-express 兼容接口**

`/api/search` · `/api/work/{id}` · `/api/tracks/{id}` · `/api/cover/{id}` · `/api/media/stream/{hash}` · `/api/review`

- 自动配置读取 KikoFlu 客户端的 `server_host`、`auth_token`、`server_cookie`
- 官方后端要求浏览器 UA / Referer，已写在 `_kikoeru_headers`
- 封面、音轨、字幕地址都带 token，更换后端时要连 token 一起换
- 搜索固定带 `subtitle=1&includeTranslationWorks=true`
- 「我的标记」走 `/api/review?filter=marked`；官方后端的 `/api/favourites` 返回 404，收藏接口未开放

</details>

<details>
<summary><b>更换音声渠道（点击展开）</b></summary>

<br>

1. **只换 KikoFlu 后端：** 改配置页的 `kikoeru_url` / `kikoeru_token`，或者直接在 KikoFlu 客户端里换地址，不用改代码
2. **换成别的音声网站：** 主要改 `_kikoeru_base`、`_kikoeru_headers`、`_search_kikoeru`、`_kikoeru_load_track_rows`、`_build_kikoeru_track_mp4`，并同步修改工具描述里的「KikoFlu」字样。旧代码可以保留，新站点写在新函数里

</details>

<details>
<summary><b>改完代码怎么验证（点击展开）</b></summary>

<br>

```bash
python -m py_compile main.py
```

冒烟测试：

```python
_search_kikoeru('关键词', 5)
_kikoeru_my_reviews(5)
_kikoeru_load_track_rows(作品ID)
```

最后重启 AstrBot 或重载插件，观察工具调用日志。

> [!IMPORTANT]
> `main.py` 使用 **CRLF + UTF-8**，修改时请保留原有换行风格。

</details>

## 🙏 致谢

- 本插件从 `astrbot_plugin_bilibili_dl` 中独立出来
- [kikoeru-express](https://github.com/kikoeru-project/kikoeru-express) 以及 [KikoFlu 客户端](https://github.com/pa-jesusf/KikoFlu)（[GPL-3.0](https://github.com/pa-jesusf/KikoFlu/blob/main/LICENSE)） —— 音声后端与接口
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 机器人框架
- [FFmpeg](https://ffmpeg.org/) · [mutagen](https://github.com/quodlibet/mutagen) · [Pillow](https://github.com/python-pillow/Pillow)

## 许可证

版权所有 (C) 2026 Zdram。

本仓库的插件代码采用 [GNU Affero General Public License v3.0 或任何后续版本](LICENSE)（AGPL-3.0-or-later）。插件访问的音声、封面和字幕各自适用其权利人的许可。
