# Grok Avatar

将 QQ 用户头像转换为 Grok bot 风格的极简 2D 机器人头像，并直接发送到当前聊天。
插件提供两个输出模式：普通 Grok bot 头像，或叠加 Google 四色圆环的头像。

插件版本：`1.0.0`

## 功能

- 由 LLM 通过 `grok_avatar` Tool 直接调用，不需要聊天命令。
- 以目标 QQ 头像作为本次生成的原始图片输入。
- 使用固定的 Grok bot icon 韩文视觉规范生成头像。
- 支持 `plain` 和 `google_ring` 两种模式。
- Google 四色圆环算法内嵌在插件中，运行时不访问在线网页或依赖浏览器。
- 每次调用都是一次新的独立图片请求，不会把上一张生成图当作下一次输入。
- 生成结果自动发送到触发 Tool 的当前聊天流，并回复触发消息。

## 工作流程

```text
目标 QQ 号
    -> onebot_expand 标准 get_stranger_info 获取头像信息
    -> 下载本次原始头像
    -> 独立 POST /v1/images/edits multipart 请求
             image = 本次原始头像
             prompt = 固定 Grok bot icon 规格
    -> plain：直接发送生成结果
         google_ring：本地叠加 Google 四色圆环后发送
```

请求不会携带 `messages`、`previous_response_id`、上一轮生成图片或聊天历史。
因此连续调用会重新下载原始头像，并分别创建独立的图片生成请求。

## 安装要求

- Neo-MoFox 核心版本：`1.0.0` 或更高
- `onebot_expand` 插件已安装并启用
- 图像模型服务支持 OpenAI 兼容的 `POST /v1/images/edits` 接口
- Python 依赖：`httpx`、`pillow`

插件清单已经声明 `onebot_expand` 依赖。若协议端无法通过标准用户信息接口返回头像，
插件会回退到 QQ 公开头像地址 `q1.qlogo.cn`。

## 配置

插件配置文件：

```text
config/plugins/grok_avatar/config.toml
```

最小配置示例：

```toml
[plugin]
enabled = true
tool_enabled = true

[api]
base_url = "https://your-image-api.example.com/v1"
api_key = "sk-your-key"
model = "your-image-model"
timeout = 180.0

[generation]
size = 1024
output_size = 640
jpeg_quality = 90

[frame]
border_ratio = 0.04
gap_ratio = 0.02

```

### 配置字段

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `plugin.enabled` | `true` | 是否启用插件 |
| `plugin.tool_enabled` | `true` | 是否注册 LLM Tool |
| `api.base_url` | 空 | OpenAI 兼容 API 根地址或完整地址 |
| `api.api_key` | 空 | Bearer API Key，可留空取决于服务端配置 |
| `api.model` | 空 | 图像模型名称，必须支持图片编辑输入 |
| `api.timeout` | `180.0` | 图像请求超时时间，单位秒 |
| `generation.size` | `1024` | 请求模型生成的正方形尺寸 |
| `generation.output_size` | `640` | `google_ring` 输出图像边长 |
| `generation.jpeg_quality` | `90` | 圆环模式输出 JPEG 质量 |
| `frame.border_ratio` | `0.04` | Google 彩色圆环宽度比例 |
| `frame.gap_ratio` | `0.02` | 圆环与头像之间白色间隔比例 |

`generation.prompt` 默认内置用户指定的韩文 Grok bot icon 规格。插件会将它原样发送给图像模型，
不会自动追加、翻译或拼接其他提示词。除非确实需要修改模型行为，否则不要改动该字段。

## LLM Tool

Tool 名称：`grok_avatar`

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `mode` | `plain` / `google_ring` | `plain` 直接发送；`google_ring` 叠加四色圆环后发送 |
| `qq_number` | 字符串，可选 | 目标 QQ 号。省略时使用当前聊天流触发消息的发送者 |
| `source` | `avatar` / `chat_history` / `specified` | 默认头像；聊天原图；指定图片 |
| `image` | 字符串，可选 | 指定图片 HTTP(S) 直链、data URL 或 base64 |
| `message_id` | 字符串，可选 | 原图所在消息 ID，仅查当前聊天流 |
| `history_index` | 非负整数 | 默认 0；从新到旧的图片索引，指定消息 ID 时为该条消息的图片索引 |
| `mention_qq` | 字符串，可选 | 结果 @ 的 QQ；头像模式默认目标 QQ，空字符串禁止 @ |
| `reply_to` | 字符串，可选 | 要引用的消息 ID，不是成员 QQ；默认不引用请求者 |

聊天原图优先使用触发消息中的图片数据，再按 `image_id` 从 MoFox 核心媒体缓存读取
落盘原图，随后尝试消息直链、OneBot `get_msg` 中的图片 URL。不把 MoFox 图片哈希
当作 QQ 的 `file` 标识。默认历史选图跳过机器人消息，避免再次修改上一张生成结果。
缓存和链接均失效时提示重新发送原图，不用文字描述代替图片生成。

群友 A 请求给 B 生成头像时，传入 B 的 `qq_number` 即默认 @B，不再引用 A 的请求。
头像对象与接收对象不同时，可单独传 `mention_qq`。当前 @ 和图片分别发送两条消息；
私聊不发送群提及。引用消息与 @成员是独立概念，不应把 QQ 号写入 `reply_to`。

例如将某条消息的第一张原图生成头像并给 B：

```json
{"source":"chat_history","message_id":"原图消息ID","history_index":0,"mention_qq":"B的QQ号","mode":"plain"}
```

示例调用：

```json
{
    "mode": "google_ring",
    "qq_number": "3863596004"
}
```

省略 `qq_number` 时：

- 私聊：默认使用当前私聊对象；
- 群聊：默认使用触发当前回合消息的发送者；
- 如果 LLM 要转换群内其他成员，应显式传入 QQ 号。

## 手动命令

插件同时注册 `/grok_avatar` 命令，适合明确指定对象或图片时使用：

```text
/grok_avatar user <QQ号> [plain|google_ring]
/grok_avatar image [消息ID] [图片序号] [接收QQ] [plain|google_ring]
```

示例：

```text
/grok_avatar user 551033763 plain
/grok_avatar image
/grok_avatar image -1902180316 0 3786449228 plain
```

`user` 直接使用指定 QQ 头像。`image` 使用聊天记录原图，图片序号从 `0` 开始；
不填消息 ID 时优先使用命令回复的消息，回复消息不在本地最近记录时会通过消息 API 回查。
命令默认不引用触发命令的消息；群聊中指定接收 QQ 时会先单独发送 @，再发送生成图片。

## Google 圆环实现

`google_frame.py` 内嵌了 [Google Avatar Frame Generator](https://github.com/2010384626/Google-Avatar-Frame-Generator)
的本地算法，不会请求该项目的在线页面。

内嵌参数包括：

- Google 红、蓝、绿、黄四色；
- 标定接缝角度：`206°`、`314°`、`48°`、`138°`；
- 圆环宽度比例：`0.04`；
- 白色间隔比例：`0.02`；
- 源图居中裁剪为正方形并进行圆形遮罩。

## 故障排查

### 模型提示不支持图片编辑

确认配置的模型和上游支持：

```text
POST /v1/images/edits
Content-Type: multipart/form-data
```

该插件不会退回到无图片的 `images/generations`，因为那样无法保证模型使用目标头像。

### 头像获取失败

插件首先调用 `onebot_expand:service:account_service` 的标准 `get_stranger_info`。
如果响应没有 `avatar`、`avatar_url`、`avatarUrl` 或 `user_avatar` 字段，
插件会使用：

```text
https://q1.qlogo.cn/g?b=qq&nk=<QQ号>&s=640
```

SnowLuma 不支持的 `get_qq_avatar` 扩展不会被调用。

### 生成多次却像修改上一张图

检查上游是否自行维护了会话状态。插件每次请求均为新的 HTTP multipart 请求，
只上传本次重新下载的原始头像和固定提示词，不发送聊天历史或上一张生成结果。

## 开发与测试

在 Neo-MoFox 根目录运行：

```powershell
& ".\.venv\Scripts\python.exe" -m pytest plugins/grok_avatar/tests -q -p no:randomly --no-cov
```

测试覆盖清单、提示词锚点、Google 圆环像素布局、URL 构造、图片格式识别、
独立 multipart 请求契约以及 Tool schema。

## 许可证与致谢

- 本插件：AGPL-3.0
- Google 圆环算法参考：[2010384626/Google-Avatar-Frame-Generator](https://github.com/2010384626/Google-Avatar-Frame-Generator)
