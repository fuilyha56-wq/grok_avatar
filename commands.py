"""grok_avatar 手动命令。"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_custom, send_image
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from .config import GrokAvatarConfig
from .service import GrokAvatarService

logger = get_logger("grok_avatar.command")
_SERVICE_SIGNATURE = "grok_avatar:service:grok_avatar"


class GrokAvatarCommand(BaseCommand):
    """手动生成 Grok bot 风格头像。"""

    name = "grok_avatar"
    description = "手动从 QQ 头像或聊天图片生成头像"
    command_prefix = "/"
    permission_level = PermissionLevel.USER

    async def execute(self, message_text: str) -> tuple[bool, str]:
        """路由并执行手动头像命令。"""
        return await super().execute(message_text)

    def _service(self) -> GrokAvatarService | None:
        from src.app.plugin_system.api import service_api

        service = service_api.get_service(_SERVICE_SIGNATURE)
        return service if isinstance(service, GrokAvatarService) else None

    def _config(self) -> GrokAvatarConfig | None:
        config = self.plugin.config
        return config if isinstance(config, GrokAvatarConfig) else None

    async def _send_result(
        self,
        result: str,
        recipient: str,
    ) -> tuple[bool, str]:
        if recipient and getattr(self._message, "chat_type", "") == "group":
            mentioned = await send_custom(
                content=recipient,
                message_type="at",
                stream_id=self.stream_id,
                processed_plain_text=f"@{recipient}",
            )
            if not mentioned:
                return False, "目标成员提及发送失败。"
        sent = await send_image(image_data=result, stream_id=self.stream_id)
        return (True, "头像已发送。") if sent else (False, "图片发送失败。")

    def _mode(self, value: str) -> str | None:
        mode = value.strip() or "plain"
        return mode if mode in {"plain", "google_ring"} else None

    @cmd_route()
    async def handle_help(self) -> tuple[bool, str]:
        return True, (
            "用法：\n"
            "/grok_avatar user <QQ号> [plain|google_ring]\n"
            "/grok_avatar image [消息ID] [图片序号] [接收QQ] [plain|google_ring]\n"
            "image 省略消息ID时，优先使用命令回复的消息；图片序号从 0 开始。"
        )

    @cmd_route("user")
    async def handle_user(self, qq_number: str = "", mode: str = "plain") -> tuple[bool, str]:
        """使用指定 QQ 头像生成。"""
        if not qq_number.isdigit():
            return False, "用法：/grok_avatar user <QQ号> [plain|google_ring]"
        selected_mode = self._mode(mode)
        if selected_mode is None:
            return False, "mode 必须是 plain 或 google_ring。"
        config = self._config()
        service = self._service()
        if config is None or service is None:
            return False, "grok_avatar 服务或配置未就绪。"
        ok, result = await service.make_avatar(qq_number, config, selected_mode)
        if not ok:
            return False, f"生成失败：{result}"
        return await self._send_result(result, qq_number)

    @cmd_route("image")
    async def handle_image(
        self,
        message_id: str = "",
        history_index: int = 0,
        mention_qq: str = "",
        mode: str = "plain",
    ) -> tuple[bool, str]:
        """使用当前聊天记录中的图片生成。"""
        if history_index < 0:
            return False, "图片序号必须是非负整数。"
        if mention_qq and not mention_qq.isdigit():
            return False, "接收 QQ 必须是纯数字。"
        selected_mode = self._mode(mode)
        if selected_mode is None:
            return False, "mode 必须是 plain 或 google_ring。"
        config = self._config()
        service = self._service()
        if config is None or service is None:
            return False, "grok_avatar 服务或配置未就绪。"
        try:
            image_bytes = await service.resolve_chat_image(
                self.stream_id,
                history_index=history_index,
                message_id=message_id or None,
                trigger_message=self._message,
                sender_id=str(getattr(self._message, "sender_id", "") or "") or None,
            )
            ok, result = await service.make_avatar_from_bytes(image_bytes, config, selected_mode)
        except Exception as error:
            logger.error("手动命令获取原图失败", exc_info=error)
            return False, f"原图获取失败：{error}"
        if not ok:
            return False, f"生成失败：{result}"
        return await self._send_result(result, mention_qq)
