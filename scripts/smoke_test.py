# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""lumin-chat 本地基础冒烟测试。"""

import json
import os
import tempfile
import sys
from copy import deepcopy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.document_library import KnowledgeBaseClient
from src.license_guard import generate_license_document, validate_license_document
from src.memory_store import MemoryStore
from src.agent import LuminChatAgent
from src.batch_runner import BatchTaskRunner
from src.models import LLMResponse, ToolCall
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
    runtime_config = deepcopy(runtime_config)
    for model_config in runtime_config.get("ai", {}).values():
        if isinstance(model_config, dict) and not model_config.get("api_key"):
            model_config["api_key"] = "smoke-test-key"

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

    with tempfile.TemporaryDirectory(prefix="lumin-chat-edit-") as edit_dir:
        edit_file = Path(edit_dir) / "sample.py"
        edit_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        replace_result = executor.replace_in_file(str(edit_file), "beta", "beta-updated")
        require(replace_result.ok, f"replace_in_file failed: {replace_result.output}")
        require("beta-updated" in edit_file.read_text(encoding="utf-8"), "replace_in_file did not update target text")

        insert_result = executor.insert_in_file(str(edit_file), "inserted-line", line_number=2)
        require(insert_result.ok, f"insert_in_file failed: {insert_result.output}")
        inserted_lines = edit_file.read_text(encoding="utf-8").splitlines()
        require(inserted_lines[1] == "inserted-line", f"insert_in_file inserted at wrong position: {inserted_lines}")

        find_result = executor.find_files(pattern="**/*.py", path=edit_dir)
        require(find_result.ok, f"find_files failed: {find_result.output}")
        find_payload = json.loads(find_result.output)
        require(any(item["path"].endswith("sample.py") for item in find_payload), f"find_files missing sample.py: {find_payload}")

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

    hostname = os.uname().nodename
    license_payload = {
        "subject": "lumin-chat",
        "issued_to": "smoke-test",
        "expires_at": "2099-01-01T00:00:00Z",
        "hostnames": [hostname],
    }
    license_doc = generate_license_document(license_payload, "smoke-secret")
    license_ok = validate_license_document(license_doc, "smoke-secret", current_hostname=hostname)
    require(license_ok.ok, f"license validation failed: {license_ok.message}")
    broken_license = dict(license_doc)
    broken_license["signature"] = "broken"
    license_fail = validate_license_document(broken_license, "smoke-secret", current_hostname=hostname)
    require(not license_fail.ok, "invalid license should be rejected")

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
    prompt_messages = agent._build_messages_for_model("")
    require(
        any("当前主机基础信息" in str(message.get("content", "")) for message in prompt_messages if message.get("role") == "system"),
        f"host context missing from prompt messages: {prompt_messages}",
    )
    require(
        any("当前工作区摘要" in str(message.get("content", "")) for message in prompt_messages if message.get("role") == "system"),
        f"workspace context missing from prompt messages: {prompt_messages}",
    )

    workspace_overview = agent.workspace_overview()
    require("sample_files" in workspace_overview or "root" in workspace_overview, f"workspace overview mismatch: {workspace_overview}")

    git_status_text = agent.git_status()
    require("repo_root" in git_status_text or "当前路径不在 git 仓库中" in git_status_text, f"git status output mismatch: {git_status_text}")

    original_session_id = agent.session.session_id
    new_session_id = agent.create_new_session()
    require(new_session_id != original_session_id, "new session id should be different")
    require(agent.memory_summary("tl3588 部署") == "当前会话还没有沉淀长期记忆。", "new session should start with empty memory")
    switched_back = agent.switch_session(original_session_id)
    require(switched_back == original_session_id, "switch_session should return original session id")
    require("tl3588" in agent.memory_summary("tl3588 部署"), "switched session should recover previous memory")

    class FakeAI:
        def __init__(self):
            self.calls = 0

        def call(self, messages, tools=None, stream=False, on_reasoning=None, on_content=None):
            del messages, tools, stream, on_reasoning, on_content
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    success=True,
                    tool_calls=[ToolCall(id="blank-tool", name="run_shell_command", arguments={"command": "printf recovered"})],
                )
            if self.calls in {2, 3}:
                return LLMResponse(success=True, content="", tool_calls=[])
            return LLMResponse(success=True, content="已从空响应中自动恢复", tool_calls=[])

    stalled_agent = LuminChatAgent(
        config=runtime_config,
        ui=TerminalUI(show_thinking=False),
        model_level=1,
        approval_policy="auto",
        workdir=os.getcwd(),
    )
    stalled_agent.ai = FakeAI()
    stalled_result = stalled_agent.run_with_trace("执行一个会触发空响应恢复的任务")
    require(stalled_result["success"] is True, f"empty response recovery should succeed: {stalled_result}")
    require("自动恢复" in stalled_result["content"], f"empty response recovery content mismatch: {stalled_result}")

    permission_result = executor.execute(
        ToolCall(
            id="permission-write",
            name="write_file",
            arguments={"path": "/root/Secure_Boot_Documentation.md", "content": "test"},
        )
    )
    if os.geteuid() != 0:
        require(not permission_result.ok, "permission error should be reported instead of raising")

    hidden_ui = TerminalUI(show_thinking=False)
    hidden_text = hidden_ui._strip_hidden_thinking("<think>内部推理</think>最终回答")
    require(hidden_text == "最终回答", f"hidden thinking strip failed: {hidden_text}")
    hidden_text_without_open = hidden_ui._strip_hidden_thinking("内部推理</think>最终回答")
    require(hidden_text_without_open == "最终回答", f"hidden thinking strip without open tag failed: {hidden_text_without_open}")

    with tempfile.TemporaryDirectory(prefix="lumin-chat-batch-") as batch_dir:
        task_file = Path(batch_dir) / "tasks.json"
        report_dir = Path(batch_dir) / "reports"
        task_file.write_text(
            json.dumps(
                [
                    {"task": "第一个任务", "new_session": True},
                    {"task": "第二个任务", "new_session": False},
                    {"task": "失败任务", "new_session": True},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        class FakeAgent:
            def __init__(self):
                self._index = 0
                self.cwd = "/tmp"
                self.session = type("Session", (), {"session_id": "session-1"})()

            def create_new_session(self) -> str:
                self._index += 1
                self.session = type("Session", (), {"session_id": f"session-{self._index + 1}"})()
                return self.session.session_id

            def run_with_trace(self, user_input: str) -> dict:
                if user_input == "失败任务":
                    return {
                        "success": False,
                        "content": "",
                        "error": "模拟失败",
                        "tool_records": [{"name": "run_shell_command", "arguments": {"command": "false"}, "ok": False, "output": "exit 1"}],
                        "session_id": self.session.session_id,
                        "cwd": self.cwd,
                    }
                return {
                    "success": True,
                    "content": f"已完成: {user_input}",
                    "error": "",
                    "tool_records": [{"name": "run_shell_command", "arguments": {"command": "printf ready"}, "ok": True, "output": "ready"}],
                    "session_id": self.session.session_id,
                    "cwd": self.cwd,
                }

        fake_agent = FakeAgent()
        runner = BatchTaskRunner(agent_factory=lambda: fake_agent, report_dir=str(report_dir))
        batch_results = runner.run_file(str(task_file))
        require(len(batch_results) == 3, f"batch result count mismatch: {batch_results}")
        require(batch_results[1]["session_id"] == batch_results[0]["session_id"], f"batch session reuse mismatch: {batch_results}")
        require(batch_results[2]["success"] is False, f"batch failure not captured: {batch_results}")
        require(Path(batch_results[0]["report_path"]).exists(), f"batch report missing: {batch_results}")

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

        mkdir_result = executor.ssh_execute_command(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            command=f"mkdir -p {remote_dir}",
            timeout_seconds=30,
        )
        require(mkdir_result.ok, f"ssh_execute_command mkdir failed: {mkdir_result.output}")

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

        exists_result = executor.ssh_execute_command(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            command=f"test -f {remote_dir}/generated.txt && printf exists",
            timeout_seconds=30,
        )
        require(exists_result.ok, f"ssh_execute_command exists failed: {exists_result.output}")
        require("exists" in json.loads(exists_result.output).get("stdout", ""), f"ssh exists unexpected: {exists_result.output}")

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

        remove_result = executor.ssh_execute_command(
            host=ssh_host,
            port=int(os.getenv("LUMIN_CHAT_TEST_SSH_PORT", "22")),
            username=ssh_user,
            password=os.getenv("LUMIN_CHAT_TEST_SSH_PASSWORD", ""),
            command=f"rm -rf {remote_dir}",
            timeout_seconds=30,
        )
        require(remove_result.ok, f"ssh_execute_command remove failed: {remove_result.output}")

    print("smoke_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
