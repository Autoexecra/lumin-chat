"""lumin-chat 本地基础冒烟测试。"""

import json
import os
import tempfile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.document_library import KnowledgeBaseClient
from src.models import ToolCall
from src.toolkit import ToolExecutor


TEST_CONFIG = {
    "command_policy": {
        "mode": "blacklist",
        "blacklist": ["shutdown", "reboot", "rm -rf /"],
        "whitelist": ["pwd", "printf", "export"],
    },
    "knowledge_base": {"enabled": False},
}


def require(condition: bool, message: str) -> None:
    """统一断言输出，便于在远端脚本中快速失败。"""

    if not condition:
        raise AssertionError(message)


def main() -> int:
    """验证环境、命令策略、持久 shell 和知识库连通性。"""

    executor = ToolExecutor(cwd=os.getcwd(), config=TEST_CONFIG, approval_policy="auto")
    runtime_config = load_config(str(PROJECT_ROOT / "config.json"))

    env_result = executor.get_environment()
    env_payload = json.loads(env_result.output)
    require(env_payload["cwd"] == str(Path(os.getcwd()).resolve()), "environment cwd mismatch")

    with tempfile.TemporaryDirectory(prefix="lumin-chat-smoke-"):
        raw_write = executor.execute(
            ToolCall(
                id="raw-write",
                name="write_file",
                arguments={
                    "raw": '{"path": "raw-parser.txt", "content": "line1\\nline2"',
                },
            )
        )
        require(raw_write.ok, f"raw write parse failed: {raw_write.output}")
        written = Path(executor.cwd) / "raw-parser.txt"
        require(written.exists(), "raw parser did not create file")
        written.unlink(missing_ok=True)

    pwd_result = executor.run_shell_command("pwd" if os.name != "nt" else "Get-Location | Select-Object -ExpandProperty Path")
    require(pwd_result.ok, f"pwd failed: {pwd_result.output}")

    if os.name != "nt":
        blocked = executor.run_shell_command("shutdown now")
        require(not blocked.ok, "blacklist did not block shutdown")

        export_result = executor.run_shell_command("export LUMIN_CHAT_SMOKE=ready")
        require(export_result.ok, f"export failed: {export_result.output}")

        echo_result = executor.run_shell_command("printf '%s' \"$LUMIN_CHAT_SMOKE\"")
        require(echo_result.ok, f"echo failed: {echo_result.output}")
        echo_payload = json.loads(echo_result.output)
        require("ready" in echo_payload["stdout"], f"persistent env missing: {echo_payload}")

        cd_result = executor.change_directory("/tmp")
        require(cd_result.ok, f"cd failed: {cd_result.output}")

        pwd_after_cd = executor.run_shell_command("pwd")
        require(pwd_after_cd.ok, f"pwd after cd failed: {pwd_after_cd.output}")
        pwd_payload = json.loads(pwd_after_cd.output)
        require(pwd_payload["cwd"] == "/tmp", f"persistent cwd mismatch: {pwd_payload}")
        require(pwd_payload.get("persistent_shell") is True, f"persistent shell not used: {pwd_payload}")

    kb_client = KnowledgeBaseClient(runtime_config)
    if kb_client.enabled:
        kb_docs = kb_client.list_documents(keyword="安全", limit=5)
        require(isinstance(kb_docs, list) and len(kb_docs) >= 1, "knowledge base listing failed")

    ssh_host = os.getenv("LUMIN_CHAT_TEST_SSH_HOST", "")
    ssh_user = os.getenv("LUMIN_CHAT_TEST_SSH_USER", "")
    if ssh_host and ssh_user:
        ssh_result = executor.ssh_execute_command(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            command=os.getenv("LUMIN_CHAT_TEST_SSH_COMMAND", "printf ready"),
            timeout_seconds=30,
        )
        require(ssh_result.ok, f"ssh_execute_command failed: {ssh_result.output}")
        ssh_payload = json.loads(ssh_result.output)
        require("ready" in ssh_payload.get("stdout", ""), f"unexpected ssh stdout: {ssh_payload}")

    print("smoke_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
