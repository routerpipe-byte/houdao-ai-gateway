#!/usr/bin/env python3
"""Run the inventory monitor while keeping each stock event to one email.

The monitor assigns each alert issue to the owner. GitHub already emails on
assignment, so this adapter removes the redundant @mention from the issue body
before submission; otherwise GitHub sends both an assignment and mention email.
"""

from __future__ import annotations

import sys
from typing import Any

import monitor

_original_github_request = monitor.github_request


def remove_redundant_mention(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    body = sanitized.get("body")
    assignees = sanitized.get("assignees")
    if not isinstance(body, str) or not isinstance(assignees, list) or not assignees:
        return sanitized

    assignee = str(assignees[0])
    for prefix in (f"@{assignee}，", f"@{assignee},", f"@{assignee} "):
        if body.startswith(prefix):
            sanitized["body"] = body[len(prefix):].lstrip()
            break
    return sanitized


def github_request_without_duplicate_email(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    if path == "/issues" and method.upper() == "POST" and isinstance(payload, dict):
        payload = remove_redundant_mention(payload)
    return _original_github_request(path, method=method, payload=payload)


def self_test() -> None:
    sample = {
        "title": "test",
        "body": "@routerpipe-byte，库存监控检测到新车。",
        "assignees": ["routerpipe-byte"],
    }
    cleaned = remove_redundant_mention(sample)
    assert cleaned["body"] == "库存监控检测到新车。"
    assert cleaned["assignees"] == ["routerpipe-byte"]
    assert sample["body"].startswith("@routerpipe-byte")


if __name__ == "__main__":
    self_test()
    monitor.github_request = github_request_without_duplicate_email
    try:
        raise SystemExit(monitor.main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        monitor.log(f"FATAL: {type(exc).__name__}: {exc}")
        raise
