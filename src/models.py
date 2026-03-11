# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""lumin-chat 运行时共享数据模型。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ToolCall:
    """描述一次由模型发起的工具调用。"""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """封装一次 LLM 调用结果。"""

    success: bool
    content: str = ""
    reasoning_content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ToolExecutionResult:
    """封装一次工具执行结果。"""

    name: str
    ok: bool
    output: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionState:
    """保存可恢复会话的持久化状态。"""

    session_id: str
    created_at: str
    model_level: int
    approval_policy: str
    cwd: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
