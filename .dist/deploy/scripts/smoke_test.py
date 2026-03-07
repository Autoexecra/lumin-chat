import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.toolkit import ToolExecutor


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    executor = ToolExecutor(cwd=os.getcwd(), approval_policy="auto")

    env_result = executor.get_environment()
    env_payload = json.loads(env_result.output)
    require(env_payload["cwd"] == str(Path(os.getcwd()).resolve()), "environment cwd mismatch")

    pwd_result = executor.run_shell_command("pwd" if os.name != "nt" else "Get-Location | Select-Object -ExpandProperty Path")
    require(pwd_result.ok, f"pwd failed: {pwd_result.output}")

    if os.name != "nt":
        export_result = executor.run_shell_command("export COPILOT_TERM_SMOKE=ready")
        require(export_result.ok, f"export failed: {export_result.output}")

        echo_result = executor.run_shell_command("printf '%s' \"$COPILOT_TERM_SMOKE\"")
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

    print("smoke_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
