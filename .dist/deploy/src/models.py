from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    success: bool
    content: str = ""
    reasoning_content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ToolExecutionResult:
    name: str
    ok: bool
    output: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionState:
    session_id: str
    created_at: str
    model_level: int
    approval_policy: str
    cwd: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
