from __future__ import annotations

from pathlib import Path

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core import logger

try:
    from .mcp_client import WechatAIMCPError, open_mcp_client
except ImportError:
    from mcp_client import WechatAIMCPError, open_mcp_client


class WechatAIPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        mcp_url: str,
        mcp_token: str,
        timeout_seconds: float,
        reconnect_retries: int,
        reconnect_backoff_initial_seconds: float,
        reconnect_backoff_max_seconds: float,
        reconnect_backoff_multiplier: float,
        shared_media_dir: str,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.mcp_url = mcp_url
        self.mcp_token = mcp_token
        self.timeout_seconds = timeout_seconds
        self.reconnect_retries = reconnect_retries
        self.reconnect_backoff_initial_seconds = reconnect_backoff_initial_seconds
        self.reconnect_backoff_max_seconds = reconnect_backoff_max_seconds
        self.reconnect_backoff_multiplier = reconnect_backoff_multiplier
        self.shared_media_dir = shared_media_dir

    def _resolve_recipient_name(self) -> str:
        raw_message = self.message_obj.raw_message or {}
        recipient_name = raw_message.get("contact_name") or raw_message.get("talker_name")
        if recipient_name:
            return str(recipient_name)
        if getattr(self.message_obj, "group_id", None):
            return str(self.message_obj.group_id)
        return str(self.message_obj.session_id)

    def _resolve_file_path(self, image_path: str) -> str:
        if image_path.startswith("file:///"):
            return image_path[8:]
        if image_path.startswith("http://") or image_path.startswith("https://"):
            raise WechatAIMCPError(
                "Remote image URLs are not supported directly. Download the file into the shared_media_dir first."
            )
        candidate = Path(image_path)
        if candidate.is_absolute():
            return str(candidate)
        shared_path = Path(self.shared_media_dir) / candidate
        return str(shared_path)

    async def send(self, message: MessageChain):
        recipient_name = self._resolve_recipient_name()
        pending_text: list[str] = []
        pending_mentions: list[str] = []

        async with open_mcp_client(
            self.mcp_url,
            self.mcp_token,
            timeout_seconds=self.timeout_seconds,
            reconnect_retries=self.reconnect_retries,
            reconnect_backoff_initial_seconds=self.reconnect_backoff_initial_seconds,
            reconnect_backoff_max_seconds=self.reconnect_backoff_max_seconds,
            reconnect_backoff_multiplier=self.reconnect_backoff_multiplier,
        ) as client:

            async def flush_text() -> None:
                if not pending_text:
                    return
                at_user_name = pending_mentions[0] if pending_mentions else None
                body = "\n".join(part for part in pending_text if part)
                if body:
                    await client.send_text_msg(
                        recipient_name=recipient_name,
                        message=body,
                        at_user_name=at_user_name,
                    )
                pending_text.clear()
                pending_mentions.clear()

            for component in message.chain:
                if isinstance(component, Plain):
                    pending_text.append(component.text)
                    continue

                if isinstance(component, At):
                    mention_name = getattr(component, "name", None) or getattr(component, "qq", None) or getattr(component, "target", None)
                    if mention_name and not pending_mentions:
                        pending_mentions.append(str(mention_name))
                    continue

                if isinstance(component, Image):
                    await flush_text()
                    file_path = self._resolve_file_path(component.file)
                    await client.send_file_msg(recipient_name=recipient_name, file_path=file_path)
                    continue

                logger.warning("Unsupported message component for wechat-ai adapter: %s", type(component).__name__)

            await flush_text()
        await super().send(message)
