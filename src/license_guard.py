# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""运行时许可证生成与校验能力。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable


DEFAULT_LICENSE_PATH = "/etc/lumin-chat/license.json"


@dataclass
class LicenseValidationResult:
    """封装许可证校验结果。"""

    ok: bool
    message: str
    payload: Dict[str, Any] = field(default_factory=dict)


def generate_license_document(payload: Dict[str, Any], secret: str) -> Dict[str, Any]:
    """根据载荷生成带签名的许可证文档。"""

    normalized_payload = dict(payload)
    signature = _sign_payload(normalized_payload, secret)
    return {
        "payload": normalized_payload,
        "signature": signature,
    }


def validate_runtime_license(config: Dict[str, Any]) -> LicenseValidationResult:
    """按配置校验运行时许可证。"""

    license_config = config.get("license", {})
    if not bool(license_config.get("enabled", False)):
        return LicenseValidationResult(ok=True, message="license check disabled")

    secret = _resolve_secret(license_config)
    if not secret:
        return LicenseValidationResult(ok=False, message="已启用许可证校验，但未提供签名密钥")

    license_file = Path(str(license_config.get("license_file") or DEFAULT_LICENSE_PATH)).expanduser()
    if not license_file.exists():
        return LicenseValidationResult(ok=False, message=f"许可证文件不存在: {license_file}")

    try:
        document = json.loads(license_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return LicenseValidationResult(ok=False, message=f"许可证文件不是有效 JSON: {exc}")
    except OSError as exc:
        return LicenseValidationResult(ok=False, message=f"读取许可证文件失败: {exc}")

    return validate_license_document(
        document=document,
        secret=secret,
        expected_subject=str(license_config.get("subject", "lumin-chat")),
        current_hostname=socket.gethostname(),
    )


def validate_license_document(
    document: Dict[str, Any],
    secret: str,
    expected_subject: str = "lumin-chat",
    current_hostname: str | None = None,
) -> LicenseValidationResult:
    """校验单个许可证文档是否合法。"""

    if not isinstance(document, dict):
        return LicenseValidationResult(ok=False, message="许可证文档必须是对象")

    payload = document.get("payload")
    signature = str(document.get("signature") or "")
    if not isinstance(payload, dict) or not signature:
        return LicenseValidationResult(ok=False, message="许可证文档缺少 payload 或 signature")

    expected_signature = _sign_payload(payload, secret)
    if not hmac.compare_digest(signature, expected_signature):
        return LicenseValidationResult(ok=False, message="许可证签名校验失败")

    subject = str(payload.get("subject") or "").strip()
    if subject != expected_subject:
        return LicenseValidationResult(ok=False, message=f"许可证主题不匹配: {subject or 'missing'}")

    now = datetime.now(timezone.utc)
    not_before = _parse_timestamp(payload.get("not_before"))
    expires_at = _parse_timestamp(payload.get("expires_at"))
    if not_before and now < not_before:
        return LicenseValidationResult(ok=False, message=f"许可证尚未生效: {payload.get('not_before')}")
    if expires_at and now > expires_at:
        return LicenseValidationResult(ok=False, message=f"许可证已过期: {payload.get('expires_at')}")

    hostnames = _normalize_string_list(payload.get("hostnames") or payload.get("machine", {}).get("hostnames", []))
    current_host = (current_hostname or socket.gethostname()).strip().lower()
    if hostnames and current_host not in hostnames:
        return LicenseValidationResult(ok=False, message=f"当前主机 {current_host} 不在许可证允许列表中")

    return LicenseValidationResult(ok=True, message="许可证校验通过", payload=payload)


def _sign_payload(payload: Dict[str, Any], secret: str) -> str:
    """对载荷做稳定签名。"""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _resolve_secret(license_config: Dict[str, Any]) -> str:
    """从环境变量或配置中解析许可证签名密钥。"""

    secret_env = str(license_config.get("secret_env") or "").strip()
    if secret_env and os.getenv(secret_env):
        return str(os.getenv(secret_env))
    return str(license_config.get("secret") or "")


def _parse_timestamp(value: Any) -> datetime | None:
    """解析 ISO 8601 时间戳。"""

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_string_list(values: Iterable[Any]) -> list[str]:
    """将任意值序列规整为小写字符串列表。"""

    results: list[str] = []
    for item in values:
        text = str(item).strip().lower()
        if text:
            results.append(text)
    return results