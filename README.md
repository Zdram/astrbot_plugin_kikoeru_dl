# 音声下载（KikoFlu）

在 AstrBot 中搜索兼容 KikoFlu 的音声库、浏览在线标记、查看作品卡并下载音轨。每部作品一张卡，音轨按名称合并。支持带字幕的 MP4，以及内嵌同步歌词的 MP3、WAV。

## 运行环境

- AstrBot 4.29.4 及以上的 4.x 版本，Python 3.12 及以上。当前测试基线为 AstrBot 4.29.4 / Python 3.12。
- 一个 AstrBot 运行环境能访问的 kikoeru-express 兼容后端。需要登录的后端使用你自己的 token。
- 安装插件时 AstrBot 会处理 Python 依赖 Pillow 和 mutagen。系统另需 CJK 字体以生成作品卡。Windows 使用微软雅黑，Linux 优先使用 Noto Sans CJK，macOS 查找苹方。Debian 和 Ubuntu 系的容器可以安装 fonts-noto-cjk，安装位置要在容器里。
- 制作 MP4 或转换音频格式需要 FFmpeg。把 ffmpeg 放到 AstrBot 进程的 PATH 中，并确保 MP4 打包所需的 libx264、AAC 和 subtitles 滤镜可用。原格式 MP3、WAV 的下载和字幕内嵌可直接运行。
- 使用自然语言交互时，AstrBot 要配置支持工具调用的对话模型。被唤醒后的独立 RJ 号和最近一批作品卡的固定选择指令可以直接处理。
- QQ 文件和视频发送需要对应适配器与客户端支持。发布候选验证包括 OneBot v11 消息封装，真实 QQ 投递以部署环境为准。

## 安装与配置

从 AstrBot 的插件页面选择安装，填写仓库地址或上传插件 ZIP。启用插件后，打开「音声下载（KikoFlu）」的配置。

| 配置项 | 用法 |
| --- | --- |
| KikoFlu 后端地址 | 填写后端根地址，例如自己的服务域名。Docker 中的 127.0.0.1 指 AstrBot 容器本身。 |
| KikoFlu 登录 token | 仅在后端要求登录时填写。 |
| 读取本机 Windows 的 KikoFlu 配置 | 默认关闭。仅在 AstrBot 和 KikoFlu 使用同一个 Windows 用户时启用。自动读取的凭据只用于同一后端源。 |
| KikoFlu 后端代理地址 | 留空直接连接。填写时，读取请求遇到网络连接故障可回退直连。 |
| 额外媒体地址白名单 | 自托管后端的视频、音频或字幕位于独立 CDN 时，按项填写 HTTPS 源，例如 https://cdn.example.com。媒体源不会收到后端 token 或 Cookie。 |
| 默认下载格式 | MP4、MP3 或 WAV。聊天里当次指定的格式优先。 |
| 排除标签 | 每项一个完整标签名，支持后端提供的译名。命中后拦截作品卡、直查和下载。 |
| KikoFlu 作品下载目录 | 留空保存到插件数据目录下的 library 文件夹。 |

## 使用示例

- 「搜索 KikoFlu 里带中字的助眠音声」
- 「看看我的想听」「换一批」或「随机换一批」
- 单独发送 RJ 号直接查看作品卡
- 「第 2 部第 3 号，发无音效版 WAV」
- 「整部下载」将当前作品的原始文件和独立字幕保存到运行 AstrBot 的设备

作品卡会显示后端提供的全部标签。MP3 和 WAV 的字幕写入同步歌词（ID3 SYLT），播放时由支持该标签的播放器显示。MP4 将可用字幕烧录进画面。

## 代码来源

本插件由 astrbot_plugin_bilibili_dl 插件中拆出，并继续独立维护。

## 相关项目

- [KikoFlu（pa-jesusf）](https://github.com/pa-jesusf/KikoFlu) 是跨平台音声客户端，采用 [GPL-3.0](https://github.com/pa-jesusf/KikoFlu/blob/main/LICENSE)。本插件连接与 KikoFlu 兼容的后端，也可在同一 Windows 用户下读取已启用的 KikoFlu 客户端配置。

## 许可证

版权所有 (C) 2026 Zdram。

本仓库的插件代码采用 [GNU Affero General Public License v3.0 或任何后续版本](LICENSE)（AGPL-3.0-or-later）。插件访问的音声、封面和字幕各自适用其权利人的许可。
