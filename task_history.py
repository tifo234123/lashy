"""
Task history manager for Clicky.
Persists multi-step task plans, executed actions, and outcomes to ~/.clicky/task_history.json.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("clicky.history")


def _get_history_file() -> Path:
    p = Path.home() / ".clicky" / "task_history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class TaskHistory:
    """Manages persistent history of executed tasks."""

    def __init__(self, file_path: Optional[Path] = None):
        self._path = file_path or _get_history_file()

    def load_all(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            _log.warning("Failed to load task history: %s", e)
            return []

    def record_task(
        self,
        request: str,
        steps: List[Dict[str, Any]],
        status: str = "completed",
        notes: str = "",
        duration_seconds: float = 0.0,
    ) -> Dict[str, Any]:
        entry = {
            "id": int(time.time() * 1000),
            "timestamp": datetime.now().isoformat(),
            "request": request,
            "status": status,
            "notes": notes,
            "duration_seconds": round(duration_seconds, 2),
            "total_steps": len(steps),
            "steps": steps,
        }
        all_tasks = self.load_all()
        all_tasks.append(entry)
        # Keep last 100 tasks
        if len(all_tasks) > 100:
            all_tasks = all_tasks[-100:]
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(all_tasks, f, indent=2, ensure_ascii=False)
            _log.info("Recorded task to history: %s (%s)", request[:50], status)
        except Exception as e:
            _log.warning("Failed to save task history: %s", e)
        return entry

    def get_recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        tasks = self.load_all()
        return tasks[-limit:]

    def format_recent_for_prompt(self, limit: int = 3) -> str:
        recent = self.get_recent(limit)
        if not recent:
            return ""
        lines = ["RECENT COMPLETED TASKS:"]
        for t in recent:
            ts = t.get("timestamp", "")[:16].replace("T", " ")
            req = t.get("request", "")
            st = t.get("status", "")
            lines.append(f"  • [{ts}] \"{req}\" -> {st}")
        return "\n".join(lines)


# Global singleton instance
task_history = TaskHistory()
