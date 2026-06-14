from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, AsyncExitStack
from dataclasses import dataclass
import json
import threading
from typing import Any, TYPE_CHECKING
import asyncio

if TYPE_CHECKING:
    import httpx
    from mcp import ClientSession


class WechatAIMCPError(RuntimeError):
    """Raised when the remote MCP server returns an unusable response."""


@dataclass(slots=True)
class MCPToolPayload:
    status: str
    data: dict[str, Any]
    raw_text: str


@dataclass(slots=True)
class _LoopClientState:
    call_lock: asyncio.Lock
    connect_lock: asyncio.Lock
    http_client: Any | None = None
    exit_stack: AsyncExitStack | None = None
    session: Any | None = None


def _load_json_object(raw_text: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise WechatAIMCPError(f"MCP tool returned non-JSON text payload: {raw_text}") from exc

    if not isinstance(data, dict):
        raise WechatAIMCPError("MCP tool JSON payload is not an object")
    return data


def _extract_text_blocks(result: Any) -> str:
    parts: list[str] = []
    for content in result.content:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def parse_tool_payload(result: Any) -> MCPToolPayload:
    if getattr(result, "structuredContent", None):
        structured = dict(result.structuredContent)
        raw_result = structured.get("result")
        if isinstance(raw_result, str):
            data = _load_json_object(raw_result)
            status = str(data.get("status", "ok"))
            return MCPToolPayload(status=status, data=data, raw_text=raw_result)

        if isinstance(raw_result, dict):
            status = str(raw_result.get("status", structured.get("status", "ok")))
            return MCPToolPayload(status=status, data=raw_result, raw_text=json.dumps(raw_result, ensure_ascii=False))

        status = str(structured.get("status", "ok"))
        return MCPToolPayload(status=status, data=structured, raw_text=json.dumps(structured, ensure_ascii=False))

    raw_text = _extract_text_blocks(result)
    if not raw_text:
        raise WechatAIMCPError("MCP tool returned no text or structured payload")

    data = _load_json_object(raw_text)

    status = str(data.get("status", "ok"))
    return MCPToolPayload(status=status, data=data, raw_text=raw_text)


class WechatAIMCPClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._state_by_loop: dict[asyncio.AbstractEventLoop, _LoopClientState] = {}
        self._state_guard = threading.Lock()

    async def __aenter__(self) -> "WechatAIMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _get_or_create_state(self) -> _LoopClientState:
        loop = asyncio.get_running_loop()
        with self._state_guard:
            state = self._state_by_loop.get(loop)
            if state is None:
                state = _LoopClientState(call_lock=asyncio.Lock(), connect_lock=asyncio.Lock())
                self._state_by_loop[loop] = state
        return state

    async def _ensure_connected(self) -> _LoopClientState:
        state = self._get_or_create_state()
        if state.session is not None:
            return state

        async with state.connect_lock:
            if state.session is not None:
                return state
            await self._connect_state(state)
        return state

    async def _connect_state(self, state: _LoopClientState) -> None:
        if state.session is not None:
            return

        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout_seconds,
        )
        exit_stack = AsyncExitStack()
        try:
            state.http_client = http_client
            read_stream, write_stream, _ = await exit_stack.enter_async_context(
                streamable_http_client(self.base_url, http_client=http_client)
            )
            session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except Exception:
            await exit_stack.aclose()
            await http_client.aclose()
            state.http_client = None
            raise

        state.exit_stack = exit_stack
        state.session = session

    async def connect(self) -> None:
        await self._ensure_connected()

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_guard:
            state = self._state_by_loop.pop(loop, None)

        if state is None:
            return

        exit_stack = state.exit_stack
        http_client = state.http_client
        state.exit_stack = None
        state.http_client = None
        state.session = None

        if exit_stack is not None:
            await exit_stack.aclose()
            return

        if http_client is not None:
            await http_client.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolPayload:
        state = await self._ensure_connected()
        if state.session is None:
            raise WechatAIMCPError("MCP client is not connected")

        async with state.call_lock:
            result = await state.session.call_tool(name, arguments=arguments or {})
        payload = parse_tool_payload(result)
        if payload.status in {
            "blocked",
            "confirmation_required",
            "error",
            "failed",
            "invalid_request",
            "not_found",
            "timeout",
            "unavailable",
        }:
            raise WechatAIMCPError(f"MCP tool {name} failed with status={payload.status}: {payload.raw_text}")
        return payload

    async def get_runtime_status(self) -> MCPToolPayload:
        return await self.call_tool("get_runtime_status")

    async def get_recent_chats(self, limit: int = 20) -> MCPToolPayload:
        return await self.call_tool("get_recent_chats", {"limit": limit})

    async def get_recent_messages(
        self,
        contact_name: str,
        limit: int = 30,
        include_non_text: bool = True,
        parse_media: bool = False,
    ) -> MCPToolPayload:
        return await self.call_tool(
            "get_recent_messages",
            {
                "contact_name": contact_name,
                "limit": limit,
                "include_non_text": include_non_text,
                "parse_media": parse_media,
            },
        )

    async def send_text_msg(
        self,
        recipient_name: str,
        message: str,
        at_user_name: str | None = None,
        wait_seconds: float = 12,
    ) -> MCPToolPayload:
        arguments: dict[str, Any] = {
            "recipient_name": recipient_name,
            "message": message,
            "wait_seconds": wait_seconds,
        }
        if at_user_name:
            arguments["at_user_name"] = at_user_name
        return await self.call_tool("send_text_msg", arguments)

    async def send_file_msg(
        self,
        recipient_name: str,
        file_path: str,
        wait_seconds: float = 12,
    ) -> MCPToolPayload:
        return await self.call_tool(
            "send_file_msg",
            {
                "recipient_name": recipient_name,
                "file_path": file_path,
                "wait_seconds": wait_seconds,
            },
        )


@asynccontextmanager
async def open_mcp_client(base_url: str, token: str, timeout_seconds: float = 30.0) -> AsyncIterator[WechatAIMCPClient]:
    client = WechatAIMCPClient(base_url=base_url, token=token, timeout_seconds=timeout_seconds)
    try:
        await client.connect()
        yield client
    finally:
        await client.close()