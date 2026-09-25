"""运行时：后台任务执行 + 事件广播（S2.1）。

对应 v0.3 设计的降级实现：
- 完整版有 EventBus（emit/bail/parallel/waterfall）与持久化事件溯源；
  S2.1 只做内存版"emit 广播"（多订阅者），事件写 SQLite 的持久化留到 S4。
- abort 中断只在 step 边界生效（由 reactor 每轮循环检查 is_aborted）。
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from dataclasses import dataclass, field

from .models import TaskState

logger = logging.getLogger(__name__)


class TaskRun:
    """单个任务的后台运行记录：事件广播 + 中断标志 + 挂起-响应（S3.3）。"""

    def __init__(self, task_id: str, storage=None):
        self.task_id = task_id
        self.abort = threading.Event()
        self._counter = itertools.count(1)  # 事件全局递增 id（契约 §3.5）
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        # S3.3 人机交互：任务挂起等待用户输入
        self._storage = storage
        self._input_event = threading.Event()
        self._pending_answer: str | None = None
        self._pending_question: str | None = None

    # ---- 事件发布（供 reactor 回调）----

    def publish(self, event_type: str, data: dict) -> None:
        msg = {"id": next(self._counter), "type": event_type, "data": data}
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put_nowait(msg)

    # ---- 订阅（供 SSE 连接）----

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ---- 中断（供 stop 接口）----

    def is_aborted(self) -> bool:
        return self.abort.is_set()

    def request_abort(self) -> None:
        self.abort.set()

    # ---- 挂起-响应（S3.3：ask_user 澄清 / 审批弹窗的等待通道）----

    def await_input(self, question: str) -> str:
        """挂起任务等待用户回答（阻塞，直到 submit_input 或用户 stop）。"""
        with self._lock:
            self._pending_question = question
            self._pending_answer = None
            self._input_event.clear()
        if self._storage is not None:
            self._storage.update_task(self.task_id, state=TaskState.WAITING)
        try:
            # 循环等待：回答到达或用户中断（stop 时 abort 被置位，唤醒等待）
            while not self._input_event.wait(timeout=0.2):
                if self.abort.is_set():
                    raise InterruptedError("用户停止了任务")
        finally:
            with self._lock:
                self._pending_question = None
        return self._pending_answer

    def submit_input(self, answer: str) -> bool:
        """API 提交用户回答；成功返回 True，任务不在等待则 False。"""
        with self._lock:
            if self._pending_question is None:
                return False
            self._pending_answer = answer
            self._input_event.set()
            return True

    def pending_question(self) -> str | None:
        with self._lock:
            return self._pending_question

    def is_waiting(self) -> bool:
        with self._lock:
            return self._pending_question is not None


class TaskManager:
    """管理所有后台任务：启动线程、停止、取运行记录。"""

    def __init__(self, storage, reactor, sse_relay=None):
        self._storage = storage
        self._reactor = reactor
        self._sse_relay = sse_relay  # S2.4：SSERelayPlugin（可选，用于把内核事件转发到任务通道）
        self._runs: dict[str, TaskRun] = {}
        self._lock = threading.Lock()

    def start(
        self,
        task_id: str,
        content: str,
        sandbox_mode: str = "read-only",
        init_messages: list | None = None,
        start_turn: int = 0,
    ) -> None:
        run = TaskRun(task_id, storage=self._storage)
        with self._lock:
            self._runs[task_id] = run
        if self._sse_relay is not None:
            self._sse_relay.set_target(task_id, run.publish)  # S3.3 修复：按 task_id 路由
        # S3.3：把当前任务的"人机交互通道"暴露给 ctx（ask_user 工具/权限插件等待输入用）
        reactor_ctx = getattr(self._reactor, "ctx", None)
        if reactor_ctx is not None:
            reactor_ctx.human_input = run
        thread = threading.Thread(
            target=self._run_task,
            args=(run, content, sandbox_mode, init_messages, start_turn),  # S4.3：续聊透传
            daemon=True,
            name=f"task-{task_id[:8]}",
        )
        thread.start()

    def _run_task(
        self,
        run: TaskRun,
        content: str,
        sandbox_mode: str,
        init_messages: list | None = None,
        start_turn: int = 0,
    ) -> None:
        """后台线程主入口：跑 ReAct，意外异常兜底为 failed。"""
        try:
            logger.info("后台线程启动 task_id=%s sandbox=%s turn=%d", run.task_id, sandbox_mode, start_turn)
            self._reactor.run(
                run.task_id,
                content,
                is_aborted=run.is_aborted,
                sandbox_mode=sandbox_mode,
                init_messages=init_messages,
                start_turn=start_turn,
            )
        except Exception as e:
            logger.exception("任务异常终止 task_id=%s", run.task_id)
            self._storage.update_task(
                run.task_id, state=TaskState.FAILED, error=f"任务异常终止: {e}"
            )
            if self._sse_relay is not None:
                self._sse_relay.set_target(run.task_id, run.publish)
            run.publish("error", {"message": f"任务异常终止: {e}", "recoverable": False})
        finally:
            if self._sse_relay is not None:
                self._sse_relay.clear_target(run.task_id)  # S3.3 修复：只清自己的
            reactor_ctx = getattr(self._reactor, "ctx", None)
            if reactor_ctx is not None:
                reactor_ctx.human_input = None

    def submit_answer(self, task_id: str, answer: str) -> bool:
        """API 层提交回答（POST /task/respond 用）。"""
        with self._lock:
            run = self._runs.get(task_id)
        if run is None:
            return False
        return run.submit_input(answer)

    def get_run_pending_question(self, task_id: str) -> str | None:
        with self._lock:
            run = self._runs.get(task_id)
        if run is None:
            return None
        return run.pending_question()

    def request_stop(self, task_id: str) -> bool:
        with self._lock:
            run = self._runs.get(task_id)
        if run is None:
            return False
        run.request_abort()
        return True

    def get_run(self, task_id: str) -> TaskRun | None:
        with self._lock:
            return self._runs.get(task_id)
