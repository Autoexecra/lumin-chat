import copy
import json
import os
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "default_model_level": 3,
        "default_approval_policy": "prompt",
        "max_tool_rounds": 8,
        "show_thinking": True,
        "session_dir": ".copilot-terminal/sessions",
    },
    "log": {
        "debug_mode": {
            "enabled": False,
            "show_llm_prompts": False,
            "show_llm_responses": False,
        }
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    config = _deep_merge(DEFAULT_CONFIG, raw)
    ai_config = config.get("ai", {})
    if not ai_config:
        raise ValueError("config.json 缺少 ai 配置")

    for name, model_config in ai_config.items():
        env_key = model_config.get("api_key_env") or os.getenv("COPILOT_TERM_API_KEY_ENV")
        if env_key and os.getenv(env_key):
            model_config["api_key"] = os.getenv(env_key)
        elif not model_config.get("api_key") and os.getenv("SILICONFLOW_API_KEY"):
            model_config["api_key"] = os.getenv("SILICONFLOW_API_KEY")

    return config


def get_model_config(config: Dict[str, Any], model_level: int) -> Dict[str, Any]:
    model_key = f"level{model_level}"
    model_config = config.get("ai", {}).get(model_key)
    if not model_config:
        raise ValueError(f"未找到模型级别: {model_key}")
    if not model_config.get("api_key"):
        raise ValueError(f"模型 {model_key} 缺少 api_key")
    return model_config
