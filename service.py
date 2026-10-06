"""grok_avatar 核心服务。

职责链：
1. 前置：通过 onebot_expand 的标准用户信息服务获取 QQ 头像 URL，随后下载字节；
2. 把本次抓取的头像字节与固定提示词（韩文 Grok bot icon 规格）
    作为一次全新的独立图片编辑请求发给配置注册的图像模型；
3. 两种模式：
   - ``plain``：直接返回生成的 Grok bot 头像；
   - ``google_ring``：生成后再叠加内嵌的 Google 四色圆环。
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import re
from typing import Any, Literal

import httpx

from src.app.plugin_system.base import BaseService
from src.kernel.logger import get_logger

from .config import GrokAvatarConfig
from .google_frame import (
    FrameError,
    apply_google_frame,
    decode_image_b64,
    encode_image_b64,
)

__all__ = ["GrokAvatarService"]

logger = get_logger("grok_avatar")

#: onebot_expand 标准账号服务签名。
_ACCOUNT_SERVICE_SIGNATURE = "onebot_expand:service:account_service"

AvatarMode = Literal["plain", "google_ring"]


class GrokAvatarService(BaseService):
    """Grok 头像生成核心 Service。

    Service 不是单例，每次 get_service() 都创建新实例；
    可变运行时状态不应放在实例上。
    """

    name: str = "grok_avatar"
    description: str = "Grok bot 风格头像生成服务（头像下载 + 图像模型 + 谷歌圆环）"
    version: str = "0.1.0"

    async def download_avatar(self, qq_number: str) -> bytes:
        """下载一张 QQ 头像字节（前置步骤）。

        优先走 onebot_expand 的标准 ``get_stranger_info``，从响应中的
        ``avatar`` / ``avatar_url`` 字段读取 URL。不会调用
        ``get_qq_avatar``：该 action 是 LLOneBot 扩展，SnowLuma 不注册，
        调用会产生 1404 unknown action。
        所有协议端都没有返回 URL 时，回退到 QQ 公开头像 URL 模式
        （``https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640``）。

        Args:
            qq_number: 目标 QQ 号（纯数字字符串）。

        Returns:
            头像图片字节。

        Raises:
            RuntimeError: 下载失败（服务不可用、URL 拿不到、请求失败）。
        """
        avatar_url = await self._fetch_avatar_url(qq_number)
        if not avatar_url:
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq_number}&s=640"
            logger.info(f"[grok_avatar] 协议端未返回头像 URL，回退公开头像 URL: {avatar_url}")

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(avatar_url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"头像下载 HTTP 错误: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"头像下载请求失败: {e}") from e

        if not resp.content:
            raise RuntimeError("头像下载结果为空。")
        return resp.content

    async def _fetch_avatar_url(self, qq_number: str) -> str | None:
        """通过 onebot_expand 获取头像 URL；不可用时返回 None。"""
        try:
            from src.app.plugin_system.api import service_api

            account_service = service_api.get_service(_ACCOUNT_SERVICE_SIGNATURE)
        except Exception as e:
            logger.warning(f"[grok_avatar] service_api 调用失败: {e}")
            return None

        try:
            qq_id = int(qq_number)
        except (TypeError, ValueError):
            raise RuntimeError(f"QQ 号必须是纯数字：{qq_number!r}") from None

        # get_stranger_info 是 OneBot v11 标准 action，SnowLuma 支持它。
        if account_service is not None:
            get_stranger_info = getattr(account_service, "get_stranger_info", None)
            if get_stranger_info is not None:
                try:
                    result: dict[str, Any] = await get_stranger_info(
                        user_id=qq_id,
                        no_cache=False,
                    )
                    url = self._extract_avatar_url(result)
                    if url:
                        return url
                    logger.debug(
                        f"[grok_avatar] get_stranger_info 未返回头像 URL: {str(result)[:200]}"
                    )
                except Exception as e:
                    logger.warning(f"[grok_avatar] get_stranger_info 调用失败: {e}")

        return None

    @staticmethod
    def _extract_avatar_url(result: Any) -> str | None:
        """从 OneBot/NapCat/SnowLuma 用户信息响应中提取头像 URL。"""
        if not isinstance(result, dict):
            return None
        data = result.get("data", result)
        if not isinstance(data, dict):
            return None
        for key in ("avatar", "avatar_url", "avatarUrl", "user_avatar"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    async def generate_grok_avatar(
        self,
        avatar_bytes: bytes,
        config: GrokAvatarConfig,
    ) -> bytes:
        """以本次抓取的头像为唯一图片输入，独立生成 Grok bot 风格头像。

        每次调用都新建 HTTP 请求，并使用 ``/images/edits`` multipart
        接口显式上传 ``avatar_bytes``。这里不使用聊天接口、不发送消息历史、
        不发送上一轮生成结果，也不复用任何模型会话状态，避免上游把请求
        解释成对上一张生成图的继续修改。

        Args:
            avatar_bytes: 原始头像字节。
            config: 插件配置实例。

        Returns:
            生成的 Grok bot 头像字节。

        Raises:
            RuntimeError: 配置缺失或模型调用失败。
        """
        api = config.api
        if not api.base_url.strip():
            raise RuntimeError("未配置图像模型 API 地址（config → api.base_url）。")
        if not api.model.strip():
            raise RuntimeError("未配置图像模型名称（config → api.model）。")

        prompt = config.generation.prompt
        if not prompt.strip():
            raise RuntimeError("生成提示词为空（config → generation.prompt）。")

        if not avatar_bytes:
            raise RuntimeError("抓取到的头像为空，拒绝发起无图片生成请求。")
        logger.info(f"生成输入 sha256={hashlib.sha256(avatar_bytes).hexdigest()} bytes={len(avatar_bytes)}")

        url = self._build_edits_url(api.base_url)
        form_data: dict[str, str] = {
            "model": api.model.strip(),
            "prompt": prompt,
            "n": "1",
            "response_format": "b64_json",
            "size": f"{config.generation.size}x{config.generation.size}",
        }
        image_filename, image_mime = self._detect_image_type(avatar_bytes)
        files = {
            "image": (image_filename, avatar_bytes, image_mime),
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api.api_key.strip():
            headers["Authorization"] = f"Bearer {api.api_key.strip()}"
        # httpx 在 multipart 请求中自动生成 boundary；手动设置 JSON
        # Content-Type 会破坏 multipart 解析，因此这里必须移除它。
        headers.pop("Content-Type", None)

        try:
            async with httpx.AsyncClient(timeout=api.timeout) as client:
                resp = await client.post(url, data=form_data, files=files, headers=headers)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"图像模型 HTTP 错误 {e.response.status_code}: {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"图像模型请求失败: {e}") from e
        except ValueError as e:
            raise RuntimeError(f"图像模型响应不是合法 JSON: {e}") from e

        b64_data = self._extract_b64(body)
        if b64_data is None:
            raise RuntimeError(
                f"图像模型响应中未找到图片数据，body 前 200: {str(body)[:200]}"
            )

        try:
            return decode_image_b64(b64_data)
        except FrameError as e:
            raise RuntimeError(f"生成图片解码失败: {e}") from e

    def _build_edits_url(self, base_url: str) -> str:
        """规范化 OpenAI 兼容 /v1/images/edits 地址。"""
        normalized = base_url.rstrip("/")
        if normalized.endswith("/images/edits"):
            return normalized
        if normalized.endswith("/images/generations"):
            normalized = normalized.rsplit("/images/generations", 1)[0]
        if normalized.endswith("/v1"):
            return f"{normalized}/images/edits"
        return f"{normalized}/v1/images/edits"

    @staticmethod
    def _detect_image_type(image_bytes: bytes) -> tuple[str, str]:
        """根据文件头确定 multipart 图片文件名与 MIME 类型。"""
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "avatar.png", "image/png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "avatar.jpg", "image/jpeg"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "avatar.gif", "image/gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "avatar.webp", "image/webp"
        # 头像下载成功但上游返回了无标准文件头时，仍交给图片 API 判定。
        return "avatar.bin", "application/octet-stream"

    def _extract_b64(self, body: Any) -> str | None:
        """从 OpenAI 图片响应中提取 b64_json（兼容 data[0].b64_json / data[0].image）。"""
        if not isinstance(body, dict):
            return None
        data = body.get("data")
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        if not isinstance(first, dict):
            return None
        for key in ("b64_json", "image"):
            value = first.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    async def make_avatar(
        self,
        qq_number: str,
        config: GrokAvatarConfig,
        mode: AvatarMode = "plain",
    ) -> tuple[bool, str]:
        """完整链路：下载头像 → 模型生成 →（可选圆环）→ 返回 base64。

        Args:
            qq_number: 目标 QQ 号。
            config: 插件配置实例。
            mode: ``plain`` 直接发送生成结果；``google_ring`` 叠加谷歌圆环后再发送。

        Returns:
            (成功标志, base64 图片数据或错误说明)。
        """
        try:
            avatar_bytes = await self.download_avatar(qq_number)
            return await self.make_avatar_from_bytes(avatar_bytes, config, mode)
        except (RuntimeError, FrameError) as e:
            return False, str(e)

    async def resolve_chat_image(
        self, stream_id: str, history_index: int = 0,
        message_id: str | None = None, trigger_message: Any = None,
        sender_id: str | None = None,
    ) -> bytes:
        """读取当前流原图，优先内存与核心落盘缓存，最后回查协议消息。"""
        from src.app.plugin_system.api import media_api, service_api, stream_api

        if history_index < 0:
            raise RuntimeError("history_index 必须是非负整数。")
        messages = await stream_api.get_stream_messages(stream_id, limit=100)
        if trigger_message is not None:
            messages = [msg for msg in messages if msg.message_id != trigger_message.message_id]
            messages.append(trigger_message)
        if not message_id:
            request = next((msg for msg in reversed(messages)
                            if not sender_id or str(getattr(msg, "sender_id", "")) == sender_id), None)
            if request is not None:
                reference = getattr(request, "reply_to", None)
                if reference:
                    message_id = str(reference)
        if message_id and not any(str(msg.message_id) == message_id for msg in messages):
            from src.core.models.message import Message

            service = service_api.get_service("onebot_expand:service:message_service")
            if service is not None:
                result = await service.get_msg(message_id=message_id)
                data = result.get("data") or {}
                media = [{"type": "image", "url": segment.get("data", {}).get("url")}
                         for segment in data.get("message", []) if segment.get("type") == "image"]
                messages.append(Message(message_id=message_id, content={"media": media}, stream_id=stream_id))
        candidates: list[tuple[Any, dict[str, Any]]] = []
        for message in reversed(messages):
            if message_id and str(message.message_id) != message_id:
                continue
            if not message_id and getattr(message, "sender_role", None) == "bot":
                continue
            if not message_id and sender_id and str(getattr(message, "sender_id", "")) != sender_id:
                continue
            content = message.content
            if isinstance(content, str):
                try:
                    content = ast.literal_eval(content)
                except (ValueError, SyntaxError):
                    content = {}
            media = content.get("media", []) if isinstance(content, dict) else []
            if not media:
                media = getattr(message, "extra", {}).get("media", [])
            images = [item for item in media if isinstance(item, dict) and item.get("type") in {"image", "emoji"}]
            if not images:
                text = str(getattr(message, "processed_plain_text", "") or "")
                images = [{"image_id": image_id} for image_id in re.findall(r"\[(?:图片|表情包)\(([^()]+)\)", text)]
            candidates.extend((message, item) for item in images)
        if history_index >= len(candidates):
            raise RuntimeError("指定消息或最近 100 条聊天记录中没有对应图片。")
        message, item = candidates[history_index]
        logger.info(f"选定原图 message_id={message.message_id} image_id={item.get('image_id', '')} sender={getattr(message, 'sender_id', '')}")
        encoded = item.get("data")
        if isinstance(encoded, str) and encoded:
            return decode_image_b64(encoded)
        image_id = item.get("image_id")
        if image_id:
            try:
                encoded = await media_api.get_media_file(str(image_id))
                if encoded:
                    return decode_image_b64(encoded)
            except Exception as error:
                logger.warning(f"原图缓存读取失败，尝试消息直链：{error}")
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            try:
                return await self.resolve_specified_image(url)
            except RuntimeError:
                pass
        service = service_api.get_service("onebot_expand:service:message_service")
        if service is not None:
            result = await service.get_msg(message_id=message.message_id)
            data = result.get("data") or {}
            segments = [seg for seg in data.get("message", []) if seg.get("type") == "image"]
            image_offset = next((index for index, candidate in enumerate(
                [candidate for owner, candidate in candidates if owner is message]
            ) if candidate is item), 0)
            if image_offset < len(segments):
                url = segments[image_offset].get("data", {}).get("url")
                if url:
                    return await self.resolve_specified_image(url)
        raise RuntimeError("原图缓存已丢失且消息直链不可用；请重新发送原图。")

    async def resolve_specified_image(self, image: str) -> bytes:
        """下载单张图片直链或解码 base64。"""
        if not image.startswith(("https://", "http://")):
            return decode_image_b64(image)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                async with client.stream("GET", image) as response:
                    response.raise_for_status()
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 20 * 1024 * 1024:
                            raise RuntimeError("图片超过 20 MB 上限。")
            if not chunks:
                raise RuntimeError("下载到的图片为空。")
            return bytes(chunks)
        except httpx.HTTPError as e:
            raise RuntimeError(f"原图下载失败：{e}") from e

    async def make_avatar_from_bytes(
        self, image_bytes: bytes, config: GrokAvatarConfig, mode: AvatarMode = "plain",
    ) -> tuple[bool, str]:
        """以本次选定的原图发起独立生成请求。"""
        try:
            grok_bytes = await self.generate_grok_avatar(image_bytes, config)

            if mode == "google_ring":
                frame = config.frame
                final_bytes = await asyncio.to_thread(
                    apply_google_frame,
                    grok_bytes,
                    output_size=config.generation.output_size,
                    border_ratio=frame.border_ratio,
                    gap_ratio=frame.gap_ratio,
                    jpeg_quality=config.generation.jpeg_quality,
                )
            else:
                final_bytes = grok_bytes

            return True, encode_image_b64(final_bytes)
        except (RuntimeError, FrameError) as e:
            logger.warning(f"[grok_avatar] 生成失败 mode={mode}: {e}")
            return False, str(e)
