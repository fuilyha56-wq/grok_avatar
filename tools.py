"""grok_avatar LLM Tool。

供 LLM 直接调用的头像生成工具：以当前聊天流绑定的触发消息发送者
（或显式传入的 QQ 号）为对象，走「头像下载 → 图像模型 → 可选谷歌圆环」
链路，生成后直接发送到当前聊天流。
"""

from __future__ import annotations

from typing import Annotated, Literal

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_custom, send_image
from src.core.components.base.tool import BaseTool

from .config import GrokAvatarConfig
from .service import GrokAvatarService

logger = get_logger("grok_avatar.tool")

#: GrokAvatarService 服务签名
_SERVICE_SIGNATURE = "grok_avatar:service:grok_avatar"


class GrokAvatarTool(BaseTool):
    """生成 Grok bot 风格头像并发送。"""

    name = "grok_avatar"
    description = (
        "把一个 QQ 号的当前头像转换成 Grok bot 风格的极简 2D 机器人图标并发送。"
        "两种模式：plain=直接发送生成结果；google_ring=生成后再叠加 Google 四色圆环发送。"
        "省略 QQ 号时默认转换当前聊天对象（私聊为对方，群聊为触发消息的发送者）。"
        "把这张图变成头像时直接使用 source=chat_history，无需先识图或反搜。"
        "给其他群成员生成头像时 qq_number 指定头像对象，mention_qq 指定接收成员；"
        "reply_to 仅接受消息 ID，不是 QQ 号，默认不引用请求者消息。"
    )

    async def execute(
        self,
        mode: Annotated[
            Literal["plain", "google_ring"],
            "plain=直接发送 Grok bot 头像；google_ring=叠加谷歌四色圆环后再发送",
        ] = "plain",
        qq_number: Annotated[
            str | None,
            "目标 QQ 号（纯数字）。省略时使用当前聊天流关联的用户",
        ] = None,
        source: Annotated[Literal["avatar", "chat_history", "specified"], "原图来源"] = "avatar",
        image: Annotated[str | None, "指定图片直链、data URL 或 base64"] = None,
        message_id: Annotated[str | None, "在当前聊天记录中定位原图消息 ID"] = None,
        history_index: Annotated[int, "图片索引，0 为最新消息第一张；指定 message_id 时为该消息第几张"] = 0,
        mention_qq: Annotated[str | None, "结果发送时 @ 的成员 QQ；头像模式默认 @qq_number；空字符串禁用提及"] = None,
        reply_to: Annotated[str | None, "明确需要引用的消息 ID，默认不引用任何消息"] = None,
    ) -> tuple[bool, str]:
        """生成头像并发送到当前聊天流。

        Args:
            mode: 输出模式。
            qq_number: 目标 QQ 号，可选。

        Returns:
            (是否成功, 结果说明)
        """
        cfg = self.plugin.config
        if not isinstance(cfg, GrokAvatarConfig) or not cfg.plugin.enabled:
            return False, "grok_avatar 插件未启用。"

        stream_id = self.get_current_stream_id()
        if not stream_id:
            return False, "无法确定发送目标聊天流。"
        if mention_qq and not mention_qq.isdigit():
            return False, "mention_qq 必须是成员 QQ 号。"

        from src.app.plugin_system.api import service_api

        service = service_api.get_service(_SERVICE_SIGNATURE)
        if not isinstance(service, GrokAvatarService):
            return False, "GrokAvatarService 服务未注册。"

        target = self._resolve_qq(qq_number)
        try:
            if source == "avatar":
                if not target or not target.isdigit():
                    return False, "请提供纯数字目标 qq_number。"
                ok, result = await service.make_avatar(target, cfg, mode)
            elif source in {"chat_history", "specified"}:
                if source == "specified":
                    if not image:
                        return False, "指定图片时必须提供 image。"
                    image_bytes = await service.resolve_specified_image(image)
                else:
                    image_bytes = await service.resolve_chat_image(
                        stream_id, history_index, message_id, self.trigger_message,
                        sender_id=(qq_number or "").strip() or None,
                    )
                ok, result = await service.make_avatar_from_bytes(image_bytes, cfg, mode)
            else:
                return False, "不支持的图片来源。"
        except Exception as error:
            logger.error("原图获取失败", exc_info=error)
            return False, f"原图获取失败：{error}"
        if not ok:
            return False, f"生成失败：{result}"

        message = self.trigger_message
        recipient = mention_qq if mention_qq is not None else (target if source == "avatar" else "")
        if recipient and getattr(message, "chat_type", "") == "group":
            mentioned = await send_custom(
                content=recipient, message_type="at", stream_id=stream_id,
                processed_plain_text=f"@{recipient}",
            )
            if not mentioned:
                return False, "目标成员提及发送失败，未发送图片。"

        sent = await send_image(
            image_data=result,
            stream_id=stream_id,
            reply_to=reply_to,
        )
        if not sent:
            return False, "图片发送失败。"
        return True, f"已发送 Grok bot 头像（{mode}，来源={source}）。"

    def _resolve_qq(self, qq_number: str | None) -> str:
        """解析目标 QQ 号：显式参数优先，其次触发消息发送者。"""
        explicit = (qq_number or "").strip()
        if explicit:
            return explicit
        message = self.trigger_message
        if message is not None:
            sender = str(getattr(message, "sender_id", "") or "").strip()
            if sender:
                return sender
        return ""
