# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""配置加载与默认值处理。"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict


SYSTEM_CONFIG_PATH = Path("/etc/lumin-chat/config.json")


DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "default_model_level": 1,
        "default_approval_policy": "auto",
        "max_tool_rounds": 8,
        "show_thinking": True,
        "session_dir": "~/.lumin-chat/sessions",
        "memory_dir": "~/.lumin-chat/memory",
        "report_dir": "~/lumin-report",
        "memory_recall_limit": 5,
        "memory_max_chars": 1600,
        "workspace_context_enabled": True,
        "workspace_context_max_depth": 2,
        "workspace_context_max_entries": 40,
    },
    "command_policy": {
        "mode": "blacklist",
        "blacklist": [
            "rm -rf /",
            "mkfs",
            "dd if=",
            "shutdown",
            "reboot",
            "poweroff",
            "halt",
            "init 0",
            "init 6",
            "systemctl poweroff",
            "systemctl reboot",
            "chmod -R 777 /",
            "chown -R root /",
            ":(){ :|:& };:",
        ],
        "whitelist": [
            "ls",
            "pwd",
            "cd",
            "cat",
            "grep",
            "find",
            "sed",
            "awk",
            "head",
            "tail",
            "echo",
            "printf",
            "env",
            "export",
            "python",
            "python3",
            "pip",
            "git",
            "make",
            "cmake",
            "scp",
            "ssh",
            "tar",
            "mkdir",
            "cp",
            "mv",
            "touch",
            "stat",
            "du",
            "df",
            "ps",
            "uname",
        ],
    },
    "model_escalation": {
        "enabled": True,
        "repeat_command_threshold": 3,
        "consecutive_error_threshold": 4,
        "upgrade_on_llm_error": True,
    },
    "knowledge_base": {
        "enabled": False,
        "host": "",
        "port": 22,
        "username": "",
        "password": "",
        "root_dir": "",
        "patterns": ["*.md", "*.txt"],
    },
    "license": {
        "enabled": False,
        "subject": "lumin-chat",
        "license_file": "/etc/lumin-chat/license.json",
        "secret_env": "LUMIN_CHAT_LICENSE_SECRET",
        "secret": "",
    },
    "deploy": {
        "host": "",
        "port": 22,
        "user": "",
        "remote_dir": "/root/lumin-chat",
    },
    "build_server": {
        "enabled": False,
        "host": "",
        "port": 22,
        "user": "root",
        "password": "",
        "remote_dir": "/root/lumin-chat-build",
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
    """递归合并默认配置与用户配置。"""

    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件并补齐默认值与环境变量覆盖。"""

    path = Path(config_path)
    raw: Dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    elif path != SYSTEM_CONFIG_PATH and not SYSTEM_CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    if SYSTEM_CONFIG_PATH.exists() and SYSTEM_CONFIG_PATH != path:
        with SYSTEM_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            system_raw = json.load(handle)
        raw = _deep_merge(raw, system_raw)

    config = _deep_merge(DEFAULT_CONFIG, raw)
    ai_config = config.get("ai", {})
    if not ai_config:
        raise ValueError("config.json 缺少 ai 配置")

    for name, model_config in ai_config.items():
        env_key = model_config.get("api_key_env") or os.getenv("LUMIN_CHAT_API_KEY_ENV")
        if env_key and os.getenv(env_key):
            model_config["api_key"] = os.getenv(env_key)
        elif not model_config.get("api_key") and os.getenv("SILICONFLOW_API_KEY"):
            model_config["api_key"] = os.getenv("SILICONFLOW_API_KEY")

    return config


def get_model_config(config: Dict[str, Any], model_level: int) -> Dict[str, Any]:
    """获取指定 level 的模型配置。"""

    model_key = f"level{model_level}"
    model_config = config.get("ai", {}).get(model_key)
    if not model_config:
        raise ValueError(f"未找到模型级别: {model_key}")
    if not model_config.get("api_key"):
        raise ValueError(f"模型 {model_key} 缺少 api_key")
    return model_config


def get_max_model_level(config: Dict[str, Any]) -> int:
    """获取当前配置中定义的最高模型级别。"""

    levels = []
    for key in config.get("ai", {}):
        if key.startswith("level") and key[5:].isdigit():
            levels.append(int(key[5:]))
    if not levels:
        raise ValueError("config.json 缺少有效的模型级别配置")
    return max(levels)
