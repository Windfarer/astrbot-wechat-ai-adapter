from __future__ import annotations

import re
from typing import Any


def should_wake_group_message(
    content: str,
    data: dict[str, Any],
    *,
    self_id: str,
    self_nickname: str | None,
    wake_all_group_messages: bool,
) -> bool:
    if wake_all_group_messages:
        return True

    for key in ("is_at_me", "mentioned_me", "at_me"):
        if data.get(key):
            return True

    mentioned_users = data.get("mentioned_users") or data.get("mentions") or data.get("at_list")
    if isinstance(mentioned_users, list):
        self_aliases = {alias for alias in (self_id, self_nickname) if alias}
        for item in mentioned_users:
            if isinstance(item, str) and item in self_aliases:
                return True
            if isinstance(item, dict):
                candidate = item.get("username") or item.get("nickname") or item.get("display_name") or item.get("name")
                if isinstance(candidate, str) and candidate in self_aliases:
                    return True

    if not content or not self_nickname:
        return False

    normalized_content = content.lstrip()
    escaped_nickname = re.escape(self_nickname)
    mention_patterns = (
        rf"^@{escaped_nickname}(?:[\s\u2005\u00a0\u3000,:：，]|$)",
        rf"^＠{escaped_nickname}(?:[\s\u2005\u00a0\u3000,:：，]|$)",
    )
    return any(re.match(pattern, normalized_content) for pattern in mention_patterns)


def should_prepend_group_at(
    is_group: bool,
    content: str,
    data: dict[str, Any],
    *,
    self_id: str,
    self_nickname: str | None,
    wake_all_group_messages: bool,
) -> bool:
    if not is_group:
        return False

    return should_wake_group_message(
        content,
        data,
        self_id=self_id,
        self_nickname=self_nickname,
        wake_all_group_messages=wake_all_group_messages,
    )