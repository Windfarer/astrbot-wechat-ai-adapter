from __future__ import annotations

import asyncio
import queue
import threading
from types import SimpleNamespace

from group_wake import should_prepend_group_at, should_wake_group_message
from mcp_client import MCPToolPayload, WechatAIMCPClient, WechatAIMCPError, parse_tool_payload
from polling_state import message_fingerprint


class FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


def make_result(*, structured=None, text: str | None = None):
    content = []
    if text is not None:
        content.append(FakeTextContent(text))
    return SimpleNamespace(content=content, structuredContent=structured)


def test_parse_tool_payload_prefers_structured_content() -> None:
    result = make_result(structured={"status": "ok", "messages": []}, text='{"status": "failed"}')

    payload = parse_tool_payload(result)

    assert isinstance(payload, MCPToolPayload)
    assert payload.status == "ok"
    assert payload.data["messages"] == []


def test_parse_tool_payload_decodes_structured_result_json_string() -> None:
    result = make_result(structured={"result": '{"status": "ok", "chats": []}'})

    payload = parse_tool_payload(result)

    assert payload.status == "ok"
    assert payload.data["chats"] == []


def test_parse_tool_payload_parses_json_text() -> None:
    result = make_result(text='{"status": "executed", "result": {"ok": true}}')

    payload = parse_tool_payload(result)

    assert payload.status == "executed"
    assert payload.data["result"] == {"ok": True}


def test_parse_tool_payload_rejects_non_json_text() -> None:
    result = make_result(text="not-json")

    try:
        parse_tool_payload(result)
    except WechatAIMCPError as exc:
        assert "non-JSON" in str(exc)
    else:
        raise AssertionError("Expected WechatAIMCPError")


def test_client_keeps_separate_state_per_event_loop() -> None:
    client = WechatAIMCPClient("http://example.com/mcp", "token")

    async def collect_state_id() -> int:
        return id(client._get_or_create_state())

    main_state_id = asyncio.run(collect_state_id())

    result_queue: queue.Queue[int] = queue.Queue()

    def runner() -> None:
        result_queue.put(asyncio.run(collect_state_id()))

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()

    other_state_id = result_queue.get_nowait()

    assert other_state_id != main_state_id


def test_group_message_with_at_prefix_triggers_wake() -> None:
    should_wake = should_wake_group_message(
        "@bot 早上好",
        {
            "sender_name": "alice",
            "content": "@bot 早上好",
        },
        self_id="wechat_ai_main",
        self_nickname="bot",
        wake_all_group_messages=False,
    )

    assert should_wake is True
    assert (
        should_prepend_group_at(
            True,
            "@bot 早上好",
            {"sender_name": "alice", "content": "@bot 早上好"},
            self_id="wechat_ai_main",
            self_nickname="bot",
            wake_all_group_messages=False,
        )
        is True
    )


def test_group_message_can_be_forced_to_wake_without_mention() -> None:
    should_wake = should_wake_group_message(
        "大家早",
        {
            "sender_name": "alice",
            "content": "大家早",
        },
        self_id="wechat_ai_main",
        self_nickname="bot",
        wake_all_group_messages=True,
    )

    assert should_wake is True


def test_message_fingerprint_uses_stable_session_key_not_display_name() -> None:
    message = {
        "sender_name": "alice",
        "timestamp": 1234567890,
        "content": "hello",
        "type": "text",
    }

    first = message_fingerprint(message, "wxid_alice")
    second = message_fingerprint(message, "Alice New Remark")

    assert first != second
