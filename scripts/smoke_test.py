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
from src.memory_store import MemoryStore
from src.agent import LuminChatAgent
from src.models import ToolCall
from src.toolkit import ToolExecutor
from src.ui import TerminalUI


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

    malformed_raw = executor.execute(
        ToolCall(
            id="malformed-write",
            name="write_file",
            arguments={
                "raw": '{"path": "/tmp/lumin-chat-malformed.txt", "content": "hello"',
            },
        )
    )
    require(malformed_raw.ok, f"malformed raw write parse failed: {malformed_raw.output}")
    Path("/tmp/lumin-chat-malformed.txt").unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="lumin-chat-memory-") as memory_dir:
        memory_store = MemoryStore(memory_dir)
        session_id = "smoke-session"
        memory_store.ensure_session(session_id, "2026-03-10T00:00:00Z")
        memory_store.record_turn(session_id, "记住默认测试板是 tl3588，部署优先用 rpm", "已记录部署偏好")
        memory_store.record_turn(session_id, "本次需要测试长期记忆召回", "长期记忆已建立")
        memory_state = memory_store.describe(session_id)
        require(memory_state["memory_count"] >= 2, f"memory count mismatch: {memory_state}")
        notes = memory_store.get_notes(session_id)
        require(any("tl3588" in item for item in notes), f"memory notes missing tl3588: {notes}")
        recalled = memory_store.build_context(session_id, "部署到 tl3588 时使用什么方式", limit=3, max_chars=600)
        require("tl3588" in recalled and "rpm" in recalled, f"memory recall mismatch: {recalled}")

    agent = LuminChatAgent(
        config=runtime_config,
        ui=TerminalUI(show_thinking=False),
        model_level=1,
        approval_policy="auto",
        workdir=os.getcwd(),
    )
    agent.memory_store.record_turn(agent.session.session_id, "记住当前会话优先部署到 tl3588", "已记录")
    agent_memory = agent.memory_summary("tl3588 部署")
    require("tl3588" in agent_memory, f"agent memory summary mismatch: {agent_memory}")
    memory_state = agent.memory_state()
    require(memory_state["memory_count"] >= 1, f"agent memory state mismatch: {memory_state}")

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

        remote_dir = os.getenv("LUMIN_CHAT_TEST_SSH_TMPDIR", "/tmp/lumin-chat-ssh-tools")
        local_upload = PROJECT_ROOT / ".dist" / "ssh-upload.txt"
        local_download = PROJECT_ROOT / ".dist" / "ssh-download.txt"
        local_upload.parent.mkdir(parents=True, exist_ok=True)
        local_upload.write_text("ssh file tool ready\n", encoding="utf-8")

        mkdir_result = executor.ssh_make_directory(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=remote_dir,
        )
        require(mkdir_result.ok, f"ssh_make_directory failed: {mkdir_result.output}")

        upload_result = executor.ssh_upload_file(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            local_path=str(local_upload),
            remote_path=f"{remote_dir}/upload.txt",
        )
        require(upload_result.ok, f"ssh_upload_file failed: {upload_result.output}")

        read_result = executor.ssh_read_file(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=f"{remote_dir}/upload.txt",
            start_line=1,
            end_line=5,
        )
        require(read_result.ok and "ready" in read_result.output, f"ssh_read_file failed: {read_result.output}")

        write_result = executor.ssh_write_file(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=f"{remote_dir}/generated.txt",
            content="generated via ssh_write_file\n",
        )
        require(write_result.ok, f"ssh_write_file failed: {write_result.output}")

        exists_result = executor.ssh_path_exists(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=f"{remote_dir}/generated.txt",
        )
        require(exists_result.ok, f"ssh_path_exists failed: {exists_result.output}")
        require(json.loads(exists_result.output).get("exists") is True, f"ssh_path_exists unexpected: {exists_result.output}")

        list_result = executor.ssh_list_directory(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=remote_dir,
            recursive=True,
            max_entries=20,
        )
        require(list_result.ok and "generated.txt" in list_result.output, f"ssh_list_directory failed: {list_result.output}")

        download_result = executor.ssh_download_file(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            remote_path=f"{remote_dir}/generated.txt",
            local_path=str(local_download),
        )
        require(download_result.ok, f"ssh_download_file failed: {download_result.output}")
        require(local_download.read_text(encoding="utf-8").startswith("generated"), "ssh_download_file content mismatch")

        remove_result = executor.ssh_remove_path(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            path=remote_dir,
        )
        require(remove_result.ok, f"ssh_remove_path failed: {remove_result.output}")

    print("smoke_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
