"""批量任务执行与报告生成。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional


class BatchTaskRunner:
    """按 JSON 文件顺序执行任务，并为每个任务生成独立报告。"""

    def __init__(self, agent_factory: Callable[[], object], report_dir: str | None = None):
        self.agent_factory = agent_factory
        self.report_dir = Path(report_dir or "~/lumin-report").expanduser()

    def run_file(self, task_file: str, report_dir: str | None = None) -> List[Dict[str, object]]:
        """读取批量任务文件并依次执行。"""

        if report_dir:
            self.report_dir = Path(report_dir).expanduser()
        self.report_dir.mkdir(parents=True, exist_ok=True)

        tasks = self._load_tasks(task_file)
        results: List[Dict[str, object]] = []
        agent = None

        for index, task in enumerate(tasks, start=1):
            if agent is None:
                agent = self.agent_factory()
            elif bool(task.get("new_session", True)):
                agent.create_new_session()

            started_at = self._utcnow()
            execution: Dict[str, object]
            try:
                execution = agent.run_with_trace(str(task["task"]))
            except Exception as exc:
                execution = {
                    "success": False,
                    "content": "",
                    "error": str(exc),
                    "tool_records": [],
                    "session_id": getattr(getattr(agent, "session", None), "session_id", ""),
                    "cwd": getattr(agent, "cwd", ""),
                }
            finished_at = self._utcnow()

            result = {
                "index": index,
                "task": str(task["task"]),
                "new_session": bool(task.get("new_session", True)),
                "started_at": started_at,
                "finished_at": finished_at,
                **execution,
            }
            report_path = self._write_report(result)
            result["report_path"] = str(report_path)
            results.append(result)

        return results

    def _load_tasks(self, task_file: str) -> List[Dict[str, object]]:
        """解析批量任务 JSON 文件。"""

        path = Path(task_file).expanduser()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("批量任务文件必须是 JSON 数组")

        tasks: List[Dict[str, object]] = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 个任务必须是对象")
            task_text = str(item.get("task", "")).strip()
            if not task_text:
                raise ValueError(f"第 {index} 个任务缺少 task 字段")
            new_session = item.get("new_session", True)
            tasks.append({"task": task_text, "new_session": bool(new_session)})
        return tasks

    def _write_report(self, result: Dict[str, object]) -> Path:
        """生成单个任务的 Markdown 报告。"""

        filename = f"{int(result['index']):02d}-{self._slugify(str(result['task']))}.md"
        report_path = self.report_dir / filename
        tool_records = list(result.get("tool_records", []))
        success = bool(result.get("success", False))
        error_text = str(result.get("error", "") or "")
        final_content = str(result.get("content", "") or "")

        lines = [
            f"# 任务报告 {int(result['index']):02d}",
            "",
            "## 1. 基本信息",
            "",
            f"- 任务描述: {result['task']}",
            f"- 是否新建会话: {'是' if result.get('new_session') else '否'}",
            f"- 会话 ID: {result.get('session_id', '')}",
            f"- 工作目录: {result.get('cwd', '')}",
            f"- 开始时间: {result.get('started_at', '')}",
            f"- 结束时间: {result.get('finished_at', '')}",
            f"- 执行结果: {'成功' if success else '失败'}",
            "",
            "## 2. 工具执行记录",
            "",
        ]

        if not tool_records:
            lines.append("本任务未触发工具调用。")
            lines.append("")
        else:
            for index, record in enumerate(tool_records, start=1):
                lines.extend(
                    [
                        f"### 2.{index} `{record.get('name', '')}`",
                        "",
                        "参数：",
                        "```json",
                        json.dumps(record.get("arguments", {}), ensure_ascii=False, indent=2),
                        "```",
                        "",
                        f"结果: {'成功' if record.get('ok') else '失败'}",
                        "",
                        "输出：",
                        "```text",
                        str(record.get("output", "") or ""),
                        "```",
                        "",
                    ]
                )

        lines.extend(["## 3. 最终输出", ""])
        if final_content:
            lines.extend(["```text", final_content, "```", ""])
        else:
            lines.extend(["本任务没有生成最终正文输出。", ""])

        if error_text:
            lines.extend(["## 4. 错误信息", "", "```text", error_text, "```", ""])

        lines.extend(
            [
                "## 5. 总结",
                "",
                self._build_summary(success, final_content, error_text, len(tool_records)),
                "",
            ]
        )

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    @staticmethod
    def _build_summary(success: bool, final_content: str, error_text: str, tool_count: int) -> str:
        """生成报告总结。"""

        if success:
            if final_content:
                return f"任务已完成，共执行 {tool_count} 次工具调用。最终结论如下：{final_content}"
            return f"任务已完成，共执行 {tool_count} 次工具调用，但模型未返回额外正文。"
        if error_text:
            return f"任务执行失败，但未中断后续批量任务。失败原因：{error_text}"
        return "任务执行失败，但未中断后续批量任务。"

    @staticmethod
    def _slugify(text: str) -> str:
        """把任务标题转换成安全文件名。"""

        cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", text.strip())
        cleaned = re.sub(r"-+", "-", cleaned).strip("-")
        return cleaned[:60] or "task"

    @staticmethod
    def _utcnow() -> str:
        """返回 UTC 时间戳字符串。"""

        return datetime.utcnow().isoformat() + "Z"