"""统一的 LLM 调用客户端。"""

import json
import logging
from typing import Callable, Dict, List, Optional

import httpx
from openai import OpenAI

from src.config_loader import get_model_config
from src.models import LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class AIClient:
    """为 lumin-chat 提供流式与非流式 LLM 调用能力。"""

    def __init__(self, config: Dict, model_level: int = 1):
        """初始化指定模型级别的 OpenAI 兼容客户端。"""

        self.config = config
        self.model_level = model_level
        self.model_config = get_model_config(config, model_level)

        self.debug_config = config.get("log", {}).get("debug_mode", {})
        self.debug_enabled = self.debug_config.get("enabled", False)
        self.show_prompts = self.debug_config.get("show_llm_prompts", False)
        self.show_responses = self.debug_config.get("show_llm_responses", False)

        httpx_client = httpx.Client(
            verify=True,
            trust_env=False,
            timeout=httpx.Timeout(90.0, connect=15.0),
        )
        self.client = OpenAI(
            api_key=self.model_config.get("api_key"),
            base_url=self.model_config.get("base_url"),
            http_client=httpx_client,
        )

    def call(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_content: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """向模型发送一次请求，并解析文本、推理流和工具调用。"""

        params = {
            "model": self.model_config.get("model"),
            "messages": messages,
            "temperature": temperature if temperature is not None else self.model_config.get("temperature", 0.1),
            "max_tokens": max_tokens if max_tokens is not None else self.model_config.get("max_tokens", 8192),
            "stream": stream,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if self.model_config.get("enable_thinking", False):
            params["extra_body"] = {"enable_thinking": True}

        if self.debug_enabled and self.show_prompts:
            logger.info("LLM request: %s", json.dumps(params, ensure_ascii=False, default=str)[:4000])

        try:
            if stream:
                return self._stream_call(params, on_reasoning=on_reasoning, on_content=on_content)

            response = self.client.chat.completions.create(**params)
            message = response.choices[0].message
            result = LLMResponse(
                success=True,
                content=message.content or "",
                reasoning_content=getattr(message, "reasoning_content", None) or "",
                tool_calls=self._parse_tool_calls(getattr(message, "tool_calls", None) or []),
                finish_reason=response.choices[0].finish_reason or "stop",
                usage={
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    "total_tokens": getattr(response.usage, "total_tokens", 0),
                },
            )
            if self.debug_enabled and self.show_responses:
                logger.info("LLM response: %s", result.content[:2000])
            return result
        except Exception as exc:
            logger.exception("LLM 调用失败")
            return LLMResponse(success=False, error=str(exc))

    def _stream_call(
        self,
        params: Dict,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_content: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """处理 OpenAI 兼容接口返回的流式响应。"""

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls_map: Dict[int, Dict] = {}
        finish_reason = "stop"

        try:
            response = self.client.chat.completions.create(**params)
            for chunk in response:
                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                reasoning_piece = getattr(delta, "reasoning_content", None) or ""
                if reasoning_piece:
                    reasoning_parts.append(reasoning_piece)
                    if on_reasoning:
                        on_reasoning(reasoning_piece)

                content_piece = delta.content or ""
                if content_piece:
                    content_parts.append(content_piece)
                    if on_content:
                        on_content(content_piece)

                for tool_call in getattr(delta, "tool_calls", None) or []:
                    index = tool_call.index
                    if index not in tool_calls_map:
                        tool_calls_map[index] = {
                            "id": tool_call.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    if tool_call.id:
                        tool_calls_map[index]["id"] = tool_call.id
                    if tool_call.function:
                        if tool_call.function.name:
                            tool_calls_map[index]["name"] = tool_call.function.name
                        if tool_call.function.arguments:
                            tool_calls_map[index]["arguments"] += tool_call.function.arguments

            return LLMResponse(
                success=True,
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts),
                tool_calls=self._parse_stream_tool_calls(tool_calls_map),
                finish_reason=finish_reason,
            )
        except Exception as exc:
            logger.exception("流式 LLM 调用失败")
            return LLMResponse(success=False, error=str(exc))

    @staticmethod
    def _parse_tool_calls(raw_tool_calls) -> List[ToolCall]:
        """解析普通响应中的工具调用。"""

        parsed: List[ToolCall] = []
        for raw in raw_tool_calls:
            arguments = getattr(raw.function, "arguments", "") or "{}"
            parsed.append(
                ToolCall(
                    id=raw.id,
                    name=raw.function.name,
                    arguments=AIClient._safe_json_loads(arguments),
                )
            )
        return parsed

    @staticmethod
    def _parse_stream_tool_calls(tool_calls_map: Dict[int, Dict]) -> List[ToolCall]:
        """解析流式响应中增量拼接出来的工具调用。"""

        parsed: List[ToolCall] = []
        for item in tool_calls_map.values():
            parsed.append(
                ToolCall(
                    id=item["id"],
                    name=item["name"],
                    arguments=AIClient._safe_json_loads(item["arguments"] or "{}"),
                )
            )
        return parsed

    @staticmethod
    def _safe_json_loads(payload: str) -> Dict:
        """尽量把模型给出的参数解析成结构化字典。"""

        candidate = payload.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict) and set(value.keys()) == {"raw"} and isinstance(value.get("raw"), str):
                    nested = value["raw"].strip()
                    if nested.startswith("{") or nested.startswith("["):
                        return AIClient._safe_json_loads(nested)
                if isinstance(value, dict):
                    return value
                return {"value": value}
            except json.JSONDecodeError:
                pass
        try:
            value = json.loads(payload)
            if isinstance(value, dict):
                return value
            return {"value": value}
        except json.JSONDecodeError:
            return {"raw": payload}
