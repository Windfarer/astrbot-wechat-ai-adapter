from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core import logger
from astrbot.core.platform.astr_message_event import MessageSesion

try:
    from .group_wake import should_prepend_group_at
    from .mcp_client import MCPToolPayload, WechatAIMCPClient, WechatAIMCPError
    from .polling_state import PollingStateStore, message_fingerprint
    from .wechat_ai_platform_event import WechatAIPlatformEvent
except ImportError:
    from group_wake import should_prepend_group_at
    from mcp_client import MCPToolPayload, WechatAIMCPClient, WechatAIMCPError
    from polling_state import PollingStateStore, message_fingerprint
    from wechat_ai_platform_event import WechatAIPlatformEvent


@dataclass(slots=True)
class SessionEnvelope:
    session_key: str
    contact_name: str
    contact_id: str
    is_group: bool


def _pick_items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


@register_platform_adapter(
    "wechat_ai",
    "wechat-ai MCP platform adapter",
    default_config_tmpl={
        "mcp_url": "http://localhost:8100/mcp",
        "mcp_token": "replace-with-real-token",
        "poll_interval_seconds": 5,
        "recent_chats_limit": 20,
        "recent_messages_limit": 30,
        "state_path": "data/plugins/astrbot-wechat-ai-adapter/state.json",
        "include_non_text": True,
        "parse_media": True,
        "shared_media_dir": "/config/exports",
        "wake_all_group_messages": False,
    },
)
class WechatAIPlatformAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.mcp_url = str(platform_config.get("mcp_url", "http://localhost:8100/mcp"))
        self.mcp_token = str(platform_config.get("mcp_token", ""))
        self.mcp_timeout_seconds = float(platform_config.get("mcp_timeout_seconds", 30))
        self.poll_interval_seconds = float(platform_config.get("poll_interval_seconds", 5))
        self.recent_chats_limit = int(platform_config.get("recent_chats_limit", 20))
        self.recent_messages_limit = int(platform_config.get("recent_messages_limit", 30))
        self.include_non_text = bool(platform_config.get("include_non_text", True))
        self.parse_media = bool(platform_config.get("parse_media", True))
        self.shared_media_dir = str(platform_config.get("shared_media_dir", "/config/exports"))
        self.wake_all_group_messages = bool(platform_config.get("wake_all_group_messages", False))
        self.state_store = PollingStateStore(str(platform_config.get("state_path", "data/plugins/astrbot-wechat-ai-adapter/state.json")))
        self.client: WechatAIMCPClient | None = None
        self._self_id = str(platform_config.get("self_id", "wechat-ai"))
        self._self_nickname: str | None = None
        self._shutdown_event = asyncio.Event()

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        if self.client is None:
            raise WechatAIMCPError("wechat-ai MCP client is not connected")

        message = AstrBotMessage()
        message.self_id = self._self_id
        message.session_id = session.session_id
        message.raw_message = {"contact_name": session.session_id}

        event = WechatAIPlatformEvent(
            message_str="",
            message_obj=message,
            platform_meta=self.meta(),
            session_id=session.session_id,
            mcp_url=self.mcp_url,
            mcp_token=self.mcp_token,
            timeout_seconds=self.mcp_timeout_seconds,
            shared_media_dir=self.shared_media_dir,
        )
        await event.send(message_chain)
        await super().send_by_session(session, message_chain)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="wechat_ai",
            description="wechat-ai MCP platform adapter",
            id=str(self.config.get("id", "wechat_ai")),
        )

    async def run(self):
        self.state_store.load()
        self._shutdown_event = asyncio.Event()
        self.client = WechatAIMCPClient(
            base_url=self.mcp_url,
            token=self.mcp_token,
            timeout_seconds=self.mcp_timeout_seconds,
        )
        await self.client.connect()

        try:
            await self.client.get_runtime_status()
            self._self_nickname = await self._load_self_nickname()
        except WechatAIMCPError as exc:
            logger.error("wechat-ai runtime check failed: %s", exc)
            raise

        try:
            while not self._shutdown_event.is_set():
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("wechat-ai polling iteration failed: %s", exc)

                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
        finally:
            if self.client is not None:
                try:
                    await self.client.close()
                finally:
                    self.client = None

    async def _load_self_nickname(self) -> str | None:
        assert self.client is not None
        payload = await self.client.call_tool("get_wechat_user_info")
        nickname = payload.data.get("nickname")
        if isinstance(nickname, str) and nickname:
            return nickname
        return None

    async def _poll_once(self) -> None:
        assert self.client is not None
        chats_payload = await self.client.get_recent_chats(limit=self.recent_chats_limit)
        for envelope in self._extract_sessions(chats_payload):
            try:
                messages_payload = await self.client.get_recent_messages(
                    contact_name=envelope.contact_name,
                    limit=self.recent_messages_limit,
                    include_non_text=self.include_non_text,
                    parse_media=self.parse_media,
                )
                await self._consume_recent_messages(envelope, messages_payload)
            except WechatAIMCPError as exc:
                logger.warning("Failed to poll session %s: %s", envelope.session_key, exc)

        self.state_store.save()

    def _extract_sessions(self, payload: MCPToolPayload) -> list[SessionEnvelope]:
        items = _pick_items(payload.data, "chats", "recent_chats", "data", "items")
        envelopes: list[SessionEnvelope] = []
        for item in items:
            contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
            contact_name = (
                contact.get("display_name")
                or contact.get("remark")
                or contact.get("nick_name")
                or contact.get("alias")
                or contact.get("username")
                or item.get("contact_name")
                or item.get("name")
                or item.get("talker_name")
                or item.get("room_name")
            )
            if not contact_name:
                continue
            is_group = bool(
                contact.get("is_chatroom")
                or item.get("is_chatroom")
                or item.get("is_group")
                or str(contact_name).endswith("@chatroom")
            )
            envelopes.append(
                SessionEnvelope(
                    session_key=str(contact.get("username") or contact_name),
                    contact_name=str(contact_name),
                    contact_id=str(contact.get("username") or contact_name),
                    is_group=is_group,
                )
            )
        return envelopes

    async def _consume_recent_messages(self, envelope: SessionEnvelope, payload: MCPToolPayload) -> None:
        messages = _pick_items(payload.data, "messages", "recent_messages", "data", "items")
        for item in reversed(messages):
            fingerprint = message_fingerprint(item, envelope.session_key)
            if self.state_store.has_seen(envelope.session_key, fingerprint):
                continue
            if self._is_self_message(item):
                self.state_store.mark_seen(envelope.session_key, fingerprint)
                continue
            abm = self.convert_message(envelope, item)
            if abm is None:
                self.state_store.mark_seen(envelope.session_key, fingerprint)
                continue
            await self.handle_msg(abm)
            self.state_store.mark_seen(envelope.session_key, fingerprint)

    def _is_self_message(self, data: dict[str, Any]) -> bool:
        if not self._self_nickname:
            return False
        sender_display = data.get("sender_display") or data.get("sender_name")
        return isinstance(sender_display, str) and sender_display == self._self_nickname

    def convert_message(self, envelope: SessionEnvelope, data: dict[str, Any]) -> AstrBotMessage | None:
        sender_name = data.get("sender_display") or data.get("sender_name") or data.get("sender") or data.get("from_user") or data.get("talker")
        sender_id = data.get("sender_username") or sender_name or envelope.contact_id
        content = data.get("content") or data.get("text") or data.get("message") or ""
        raw_message_type = data.get("type_name") or data.get("type") or data.get("msg_type") or "text"
        message_type = str(raw_message_type)
        media_path = data.get("media_path") or data.get("file_path") or data.get("path") or data.get("parsed_path")

        components: list[Any] = []
        display_text = ""

        if content:
            display_text = str(content)
            components.append(Plain(text=display_text))

        if should_prepend_group_at(
            envelope.is_group,
            display_text,
            data,
            self_id=self._self_id,
            self_nickname=self._self_nickname,
            wake_all_group_messages=self.wake_all_group_messages,
        ):
            components.insert(0, At(qq=self._self_id, name=self._self_nickname or self._self_id))

        if message_type not in {"text", "1", "文本"} and media_path:
            components.append(Image.fromFileSystem(str(media_path)))
            if not display_text:
                display_text = f"[media] {media_path}"

        if not components:
            return None

        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE if envelope.is_group else MessageType.FRIEND_MESSAGE
        abm.group_id = envelope.contact_name if envelope.is_group else ""
        abm.session_id = envelope.contact_name
        abm.message_id = str(data.get("server_id") or data.get("message_id") or data.get("local_id") or data.get("id") or "")
        abm.self_id = self._self_id
        abm.sender = MessageMember(user_id=str(sender_id), nickname=str(sender_name or envelope.contact_name))
        abm.message_str = display_text
        abm.message = components
        abm.raw_message = {**data, "contact_name": envelope.contact_name, "is_group": envelope.is_group}
        if data.get("create_time"):
            abm.timestamp = data.get("create_time")
        elif data.get("timestamp"):
            abm.timestamp = data.get("timestamp")
        return abm

    async def handle_msg(self, message: AstrBotMessage):
        if self.client is None:
            raise WechatAIMCPError("wechat-ai MCP client is not connected")

        message_event = WechatAIPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            mcp_url=self.mcp_url,
            mcp_token=self.mcp_token,
            timeout_seconds=self.mcp_timeout_seconds,
            shared_media_dir=self.shared_media_dir,
        )
        self.commit_event(message_event)

    async def terminate(self) -> None:
        self._shutdown_event.set()
