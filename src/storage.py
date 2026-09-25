"""SQLite 存储（S1 降级版：tasks + steps 两张表）。

v0.3 的会话持久化设计是事件溯源（events 表 append-only + derive_messages），
S1 按预案降级为"直接存任务状态 + 步骤记录"；S4 需要断点续做时再升级为 events 表。

S2 起任务在后台线程执行、API 线程并发读写，所有 SQLite 访问必须过同一把锁。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import StepResult, Task, TaskState, new_task_id, now_iso


class Storage:
    def __init__(self, db_path: str):
        p = Path(db_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _execute(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id    TEXT PRIMARY KEY,
                    content    TEXT NOT NULL,
                    state      TEXT NOT NULL,
                    turn       INTEGER NOT NULL DEFAULT 0,
                    step       INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error      TEXT,
                    result     TEXT,
                    sandbox_mode TEXT NOT NULL DEFAULT 'read-only'
                )
                """
            )
            # S2.3 迁移：旧库 tasks 表没有 sandbox_mode 列
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()]
            if "sandbox_mode" not in cols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN sandbox_mode TEXT NOT NULL DEFAULT 'read-only'"
                )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id       TEXT NOT NULL,
                    turn_index    INTEGER NOT NULL,
                    step_index    INTEGER NOT NULL,
                    llm_thought   TEXT,
                    tool_name     TEXT,
                    tool_arguments TEXT,
                    tool_result   TEXT,
                    status        TEXT NOT NULL,
                    timestamp     TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, id)"
            )
            # S4.1：事件溯源表——短期记忆的持久化形态（除 token 外全量事件）
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    TEXT NOT NULL,
                    seq        INTEGER NOT NULL,
                    type       TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- tasks ----------

    def create_task(self, content: str, sandbox_mode: str = "read-only") -> Task:
        task = Task(task_id=new_task_id(), content=content, sandbox_mode=sandbox_mode)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, content, state, turn, step, created_at, updated_at, error, result, sandbox_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.content,
                    task.state.value,
                    task.turn,
                    task.step,
                    task.created_at,
                    task.updated_at,
                    task.error,
                    task.result,
                    task.sandbox_mode,
                ),
            )
        return task

    def update_task(
        self,
        task_id: str,
        *,
        state: TaskState | None = None,
        turn: int | None = None,
        step: int | None = None,
        error: str | None = None,
        result: str | None = None,
    ) -> None:
        fields = ["updated_at = ?"]
        values: list = [now_iso()]
        if state is not None:
            fields.append("state = ?")
            values.append(state.value)
        if turn is not None:
            fields.append("turn = ?")
            values.append(turn)
        if step is not None:
            fields.append("step = ?")
            values.append(step)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if result is not None:
            fields.append("result = ?")
            values.append(result)
        values.append(task_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?", values)

    def get_task(self, task_id: str) -> Task | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return Task(
            task_id=row["task_id"],
            content=row["content"],
            state=TaskState(row["state"]),
            turn=row["turn"],
            step=row["step"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            result=row["result"],
            sandbox_mode=row["sandbox_mode"] or "read-only",
        )

    # ---------- steps ----------

    def append_step(self, task_id: str, step: StepResult) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO steps
                    (task_id, turn_index, step_index, llm_thought, tool_name,
                     tool_arguments, tool_result, status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step.turn_index,
                    step.step_index,
                    step.llm_thought,
                    step.tool_name,
                    json.dumps(step.tool_arguments, ensure_ascii=False) if step.tool_arguments else None,
                    json.dumps(step.tool_result.to_dict(), ensure_ascii=False) if step.tool_result else None,
                    step.status.value,
                    step.timestamp,
                ),
            )

    def list_steps(self, task_id: str, offset: int = 0, limit: int = 100) -> list[dict]:
        rows = self._execute(
            """
            SELECT turn_index, step_index, llm_thought, tool_name,
                   tool_arguments, tool_result, status, timestamp
            FROM steps WHERE task_id = ?
            ORDER BY id LIMIT ? OFFSET ?
            """,
            (task_id, limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            item = {
                "turn_index": row["turn_index"],
                "step_index": row["step_index"],
                "llm_thought": row["llm_thought"],
                "tool_name": row["tool_name"],
                "tool_arguments": json.loads(row["tool_arguments"]) if row["tool_arguments"] else None,
                "tool_result": json.loads(row["tool_result"]) if row["tool_result"] else None,
                "status": row["status"],
                "timestamp": row["timestamp"],
            }
            result.append(item)
        return result

    def count_steps(self, task_id: str) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM steps WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["n"])

    # ---------- 任务列表（S3.6 对话界面：左侧会话列表） ----------

    def list_tasks(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """按最近更新倒序返回任务列表（精简字段，供会话列表展示）。"""
        rows = self._execute(
            """
            SELECT task_id, content, state, created_at, updated_at, sandbox_mode
            FROM tasks ORDER BY updated_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_tasks(self) -> int:
        row = self._execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        return int(row["n"])

    # ---------- events（S4.1 事件溯源：短期记忆持久化） ----------

    def create_event(self, task_id: str, seq: int, ev_type: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO events (task_id, seq, type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, seq, ev_type, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def list_events(self, task_id: str) -> list[dict]:
        """按 seq 顺序返回任务的全部事件（payload 已反序列化）。"""
        rows = self._execute(
            "SELECT seq, type, payload FROM events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        return [
            {"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload"])}
            for r in rows
        ]

    def count_events(self, task_id: str) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM events WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["n"])

    def delete_task(self, task_id: str) -> None:
        """S4.4：删除会话——级联删除 tasks + steps + events（不留孤儿数据）。

        调用方需先确认任务不在运行中（executing/planning/waiting），否则后台线程会重新写库。
        """
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM steps WHERE task_id = ?", (task_id,))
            self._conn.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    def mark_interrupted(self) -> int:
        """S4.2：服务启动扫描——把残留的 executing/planning/waiting 任务标记为 interrupted。

        触发场景：任务执行中服务被杀/崩溃/重启，后台线程与 TaskRun 一起消失，
        但 SQLite 状态仍停留在 executing——如不标记，前端将永远看到"执行中"（假死）。
        返回被标记的任务数。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE tasks SET state = ?, updated_at = ?
                WHERE state IN ('executing', 'planning', 'waiting')
                """,
                (TaskState.INTERRUPTED.value, now_iso()),
            )
            return cur.rowcount
