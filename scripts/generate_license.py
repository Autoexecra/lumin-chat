# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""生成 lumin-chat 运行时许可证文件。"""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path

from src.license_guard import generate_license_document


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""

    parser = argparse.ArgumentParser(description="生成 lumin-chat 许可证文件")
    parser.add_argument("--output", default="license.json", help="输出文件路径")
    parser.add_argument("--issued-to", default="demo-user", help="许可证归属")
    parser.add_argument("--subject", default="lumin-chat", help="许可证主题")
    parser.add_argument("--expires-at", required=True, help="过期时间，例如 2027-03-11T00:00:00Z")
    parser.add_argument("--hostname", action="append", default=[], help="允许的主机名，可重复指定")
    parser.add_argument("--secret-env", default="LUMIN_CHAT_LICENSE_SECRET", help="签名密钥环境变量名")
    return parser


def main() -> int:
    """生成并写出许可证文件。"""

    args = build_parser().parse_args()
    secret = os.getenv(args.secret_env, "")
    if not secret:
        raise SystemExit(f"环境变量 {args.secret_env} 未设置，无法生成许可证")

    hostnames = args.hostname or [socket.gethostname()]
    payload = {
        "subject": args.subject,
        "issued_to": args.issued_to,
        "expires_at": args.expires_at,
        "hostnames": hostnames,
    }
    document = generate_license_document(payload, secret)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())