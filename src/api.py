"""FastAPI 应用（S1 + S2.1：后台任务 + SSE 流式 + 停止）。

实现《接口契约-S0.md》§3：
- POST /task/create   后台执行，立即返回（state=pending）
- GET  /task/status   查询状态
- POST /task/stop     请求停止（abort 在 step 边界生效）
- GET  /task/logs     历史步骤
- GET  /task/stream   SSE 事件流（历史重放 + 实时订阅 + 15s 心跳）
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Config, load_config
from .kernel.plugin import AgentContext
from .llm_client import LLMClient
from .model_registry import flatten, load_models, save_current_model
from .models import TaskState
from .permissions import PermissionPolicy
from .plugins.permission_plugin import PermissionPlugin
from .plugins.event_store_plugin import EventStorePlugin
from .plugins.repeat_guard_plugin import RepeatGuardPlugin
from .plugins.sse_relay_plugin import SSERelayPlugin
from .plugins.tool_registry import ToolRegistryPlugin
from .reactor import Reactor
from .rebuild import rebuild_messages
from .runtime import TaskManager, TaskRun
from .storage import Storage
from .tools.ask_user import AskUserTool
from .tools.file_view import FileViewTool
from .tools.shell_run import ShellRunTool

logger = logging.getLogger(__name__)

MAX_CONTENT_LEN = 10000  # 契约 §5：CONTENT_TOO_LONG 阈值

INDEX_HTML = Path(__file__).parent / "static" / "index.html"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # 禁用代理缓冲，保证逐事件推送
    "Connection": "keep-alive",
}


class CreateTaskRequest(BaseModel):
    """POST /task/create 请求体（契约 §3.1）。"""

    content: str = Field(
        ...,
        min_length=1,
        max_length=MAX_CONTENT_LEN,
        description="任务内容（自然语言描述你要 Agent 做的事）",
        examples=["读取 README-S1.md 并总结"],
    )
    sandbox_mode: Literal["on-demand", "full-access"] = "on-demand"  # 两档：按需确认 / 全部允许


class StopTaskRequest(BaseModel):
    """POST /task/stop 请求体（契约 §3.3）。"""

    task_id: str = Field(..., description="任务 ID")
    reason: str = Field("user", description="停止原因，默认 user")


class RespondTaskRequest(BaseModel):
    """POST /task/respond 请求体（S3.3，契约 §3.6）：回答 Agent 的澄清/审批请求。"""

    task_id: str = Field(..., description="任务 ID")
    answer: str = Field(..., min_length=1, max_length=2000, description="用户回答内容")

class SwitchModelRequest(BaseModel):
    """POST /models/switch 请求体（模型切换）：切换到清单中的模型。"""

    name: str = Field(..., min_length=1, max_length=100, description="目标模型名（须在 models.txt 清单中）")


def _sse(event_id: int | None, event_type: str, data: dict) -> str:
    """SSE 消息格式（契约 §3.5）。

    - 实时事件：带递增 id（浏览器 EventSource 用 Last-Event-ID 续传）
    - 历史重放/终态补发：**不带 id 行**（S3.3 修复——若多个事件 id 相同
      （如全 0），浏览器会按 Last-Event-ID 规则丢弃除第一个外的所有事件）
    """
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(config: Config | None = None, llm=None) -> FastAPI:
    """构造应用。llm 可注入（测试用 FakeLLM），默认从配置创建 LLMClient。

    S2.4 组装微内核：AgentContext + 插件注册（工具/权限/SSE 转发），
    Reactor 从 ctx 取工具、通过 EventBus 发事件，不再直接依赖横切模块。
    """
    config = config or load_config()
    models_groups = load_models(config.models_file)  # 模型切换：可用模型清单（models.txt）
    storage = Storage(config.db_path)
    # S4.2：启动扫描残留任务（服务重启/崩溃后 executing/waiting/planning → interrupted，防假死）
    n = storage.mark_interrupted()
    if n:
        logger.info("启动扫描：%d 个未完成任务已标记为 interrupted", n)
    if llm is None:
        llm = LLMClient(config)

    # ---- 微内核组装（S2.4）----
    kernel = AgentContext(config=config, storage=storage)
    tools: dict = {
        "file_view": FileViewTool(max_bytes=config.tool_output_max_bytes),
        "shell_run": ShellRunTool(max_bytes=config.tool_output_max_bytes),
        "ask_user": AskUserTool(kernel),  # S3.3：澄清通道（经 ctx.human_input 等待回答）
    }
    kernel.plugins.register(ToolRegistryPlugin(tools))
    kernel.plugins.register(PermissionPlugin(PermissionPolicy()))
    kernel.plugins.register(RepeatGuardPlugin(soft_limit=config.repeat_soft_limit, hard_limit=config.repeat_hard_limit))  # S5.1：防死循环
    sse_relay = SSERelayPlugin()
    kernel.plugins.register(sse_relay)
    kernel.plugins.register(EventStorePlugin())  # S4.1：事件溯源落库（events 表）
    kernel.start()  # setup 各插件（注册事件监听、暴露 ctx.tools）

    reactor = Reactor(llm, kernel)
    manager = TaskManager(storage, reactor, sse_relay=sse_relay)

    app = FastAPI(title="ActionAgent", version="0.2.0")

    # ---- 统一错误体（契约 §5：{code, message, detail?}）----
    @app.exception_handler(HTTPException)
    async def http_exc_handler(request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "请求参数不合法", "detail": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def internal_exc_handler(request, exc: Exception):
        # 配置类错误（缺 Key 等）给出明确信息；其余不暴露堆栈（契约 §5：INTERNAL）
        if isinstance(exc, ValueError):
            return JSONResponse(
                status_code=500,
                content={"code": "CONFIG_ERROR", "message": str(exc)},
            )
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL", "message": "服务内部错误"},
        )

    # ---- 中文控制台首页 ----
    @app.get("/", include_in_schema=False)
    def index():
        # no-cache：避免浏览器缓存旧版页面（S3.3 起前端改动频繁）
        return HTMLResponse(
            INDEX_HTML.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # ---- 3.1 发起任务（S2.1：后台执行立即返回；S2.3：应用 sandbox_mode）----
    @app.post("/api/v1/task/create")
    def create_task(payload: CreateTaskRequest):
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CONTENT", "message": "任务内容不能为空"})
        if len(content) > MAX_CONTENT_LEN:
            raise HTTPException(status_code=400, detail={"code": "CONTENT_TOO_LONG", "message": f"任务内容超过 {MAX_CONTENT_LEN} 字符"})

        task = storage.create_task(content, sandbox_mode=payload.sandbox_mode)
        logger.info("任务创建 task_id=%s content=%r sandbox=%s", task.task_id, content[:80], payload.sandbox_mode)
        manager.start(task.task_id, content, sandbox_mode=payload.sandbox_mode)  # 后台线程跑 ReAct，接口立即返回
        return storage.get_task(task.task_id).to_dict()

    # ---- 3.2 查询状态 ----
    @app.get("/api/v1/task/status")
    def get_status(task_id: str = Query(..., description="任务 ID")):
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"})
        return task.to_dict()

    # ---- 3.3 停止任务（abort 只在 step 边界生效）----
    @app.post("/api/v1/task/stop")
    def stop_task(payload: StopTaskRequest):
        task = storage.get_task(payload.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {payload.task_id}"})
        if task.state in (TaskState.DONE, TaskState.FAILED, TaskState.PAUSED):
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_STOPPABLE", "message": f"任务已处于 {task.state.value}，无法停止"})
        if not manager.request_stop(payload.task_id):
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_STOPPABLE", "message": "任务不在运行中，无法停止"})
        logger.info("任务停止请求 task_id=%s reason=%s", payload.task_id, payload.reason)
        return {"task_id": payload.task_id, "state": TaskState.PAUSED.value}

    # ---- 3.6 回答澄清/审批（S3.3：waiting 任务的唯一恢复入口）----
    @app.post("/api/v1/task/respond")
    def respond_task(payload: RespondTaskRequest):
        task = storage.get_task(payload.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {payload.task_id}"})
        if task.state != TaskState.WAITING:
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_WAITING", "message": f"任务当前状态为 {task.state.value}，不在等待输入"})
        if not manager.submit_answer(payload.task_id, payload.answer):
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_WAITING", "message": "任务已离开等待状态，无法提交回答"})
        logger.info("任务回答已提交 task_id=%s answer=%r", payload.task_id, payload.answer[:80])
        return {"task_id": payload.task_id, "state": TaskState.EXECUTING.value, "message": "回答已受理，任务继续执行"}

    # ---- S3.6：任务列表（对话界面左侧会话列表） ----
    @app.get("/api/v1/tasks")
    def list_tasks(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {
            "total": storage.count_tasks(),
            "tasks": storage.list_tasks(limit=limit, offset=offset),
        }

    # ---- S4.4：删除会话（级联删 tasks/steps/events；运行中任务拒绝） ----
    @app.delete("/api/v1/tasks/{task_id}")
    def delete_task(task_id: str):
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"})
        if task.state in (TaskState.EXECUTING, TaskState.PLANNING, TaskState.WAITING):
            raise HTTPException(status_code=409, detail={"code": "TASK_RUNNING", "message": "任务正在执行，请先停止再删除"})
        storage.delete_task(task_id)
        logger.info("会话已删除 task_id=%s（级联清 tasks/steps/events）", task_id)
        return {"task_id": task_id, "deleted": True}

    # ---- S4.3：同会话续聊（多轮对话 + 中断/停止后继续） ----
    # 已结束（done/failed/paused/interrupted）的任务，在同一会话内追加新用户消息，
    # 从 events 重建历史上下文 → 继续执行（turn+1）。运行中（executing/planning/waiting）拒绝。
    @app.post("/api/v1/task/{task_id}/continue")
    def continue_task(task_id: str, payload: CreateTaskRequest):
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"})
        if task.state in (TaskState.EXECUTING, TaskState.PLANNING, TaskState.WAITING):
            raise HTTPException(status_code=409, detail={"code": "TASK_RUNNING", "message": "任务正在执行，不能续聊"})
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CONTENT", "message": "消息内容不能为空"})
        messages = rebuild_messages(storage, reactor.tools, task_id, config, reactor.llm)  # 续聊：带滚动记忆压缩
        if messages is None:
            raise HTTPException(status_code=409, detail={"code": "CONTINUE_UNSUPPORTED", "message": "该任务没有可重建的历史（旧任务），无法续聊"})
        # 追加本轮新消息；轮次号递增
        new_turn = task.turn + 1
        messages.append({"role": "user", "content": content})
        storage.update_task(task_id, state=TaskState.EXECUTING, turn=new_turn, step=0)
        logger.info("任务续聊 task_id=%s turn=%d content=%r", task_id, new_turn, content[:80])
        manager.start(
            task_id,
            content,
            sandbox_mode=task.sandbox_mode,
            init_messages=messages,
            start_turn=new_turn,
        )
        return storage.get_task(task_id).to_dict()

    # ---- 3.4 历史日志 ----
    @app.get("/api/v1/task/logs")
    def get_logs(
        task_id: str = Query(...),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ):
        if storage.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"})
        return {
            "task_id": task_id,
            "total": storage.count_steps(task_id),
            "logs": storage.list_steps(task_id, offset=offset, limit=limit),
        }

    # ---- 3.5 SSE 事件流 ----
    @app.get("/api/v1/task/stream")
    def stream(task_id: str = Query(...), replay: bool = Query(True, description="S4.3：续聊时 replay=false 跳过历史重放（界面已有历史），只订阅实时")):
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"})
        run = manager.get_run(task_id)
        return StreamingResponse(
            _sse_generator(task_id, task, run, storage, replay=replay),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    # ---- 模型切换（模型清单 / 运行时切换；校验失败自动回滚 + 持久化）----
    @app.get("/api/v1/models")
    def list_models():
        """返回当前模型 + 可用模型分组清单（models.txt，按厂商分组，多模态在最后）。"""
        return {
            "current": llm.model,
            "groups": [{"group": g.group, "models": g.models} for g in models_groups],
        }

    @app.post("/api/v1/models/switch")
    def switch_model(payload: SwitchModelRequest):
        name = payload.name.strip()
        if name not in flatten(models_groups):
            raise HTTPException(status_code=400, detail={"code": "MODEL_NOT_IN_LIST", "message": f"模型不在清单中: {name}"})
        ok, reason = llm.verify_model(name)  # 切换前先试水（1-token），防切到不可用模型
        if not ok:
            # 校验失败 → 自动回滚（当前模型不变），返回具体原因
            raise HTTPException(status_code=400, detail={"code": "MODEL_VERIFY_FAILED", "message": reason})
        llm.set_model(name)
        save_current_model(name, base_dir=Path(config.db_path).parent)  # 持久化，重启不丢
        logger.info("模型切换成功 name=%s", name)
        return {"current": name}

    return app


async def _sse_generator(task_id: str, task, run: TaskRun | None, storage: Storage, replay: bool = True):
    """SSE 生成器：先重放 SQLite 历史，再补终态或订阅实时队列。"""
    # ---- 1. 历史重放（S4.1：优先 events 真相源；旧任务无 events 时降级 steps）----
    # S4.3：replay=False（同会话续聊）跳过——界面已保留历史，重复渲染会错乱
    # 2026-09-26 修复：原先 if events/else 在 replay=False 时 events=[] 误入 else 的 steps 重放，
    # 导致续聊仍重放历史（且 steps.turn_index 与 events 轮次不一致），跨轮污染折叠块。
    replayed_done = False  # 2026-09-26：events 重放是否已含 agent/done（决定终态是否补发 done）
    if replay:
        events = storage.list_events(task_id)
        if events:
            for ev in events:
                p = ev["payload"]
                if ev["type"] == "agent/thought":
                    yield _sse(None, "thought", {"turn": p.get("turn", 0), "step": p.get("step", 0), "text": p.get("text", ""), "task_id": task_id})
                elif ev["type"] == "tools/call":
                    yield _sse(None, "tool_call", {"turn": p.get("turn", 0), "step": p.get("step", 0), "name": p.get("name", ""), "arguments": p.get("arguments"), "task_id": task_id})
                elif ev["type"] == "tools/result":
                    yield _sse(
                        None,
                        "tool_result",
                        {
                            "turn": p.get("turn", 0),
                            "step": p.get("step", 0),
                            "ok": p.get("ok"),
                            "content_summary": (p.get("content") or "")[:500],
                            "is_denied": p.get("is_denied", False),
                            "task_id": task_id,
                        },
                    )
                elif ev["type"] == "agent/user":
                    yield _sse(None, "user", {"turn": p.get("turn", 0), "content": p.get("content", ""), "task_id": task_id})
                elif ev["type"] == "agent/done":
                    replayed_done = True
                    yield _sse(None, "done", {"result": p.get("result", ""), "task_id": task_id})
                elif ev["type"] == "memory/summary":
                    # 滚动记忆：重放时告知前端"历史某段已压缩"（提示条，不参与对话渲染）
                    yield _sse(None, "memory_compressed", {"turns": f"{p.get('turn_start', 0) + 1}-{p.get('turn_end', 0) + 1}", "task_id": task_id})
                elif ev["type"] == "agent/token_usage":
                    # 2026-09-30：历史会话打开时重放，前端累计出该会话的真实消耗
                    yield _sse(None, "token_usage", {"turn": p.get("turn", 0), "step": p.get("step", 0), "prompt": p.get("prompt", 0), "completion": p.get("completion", 0), "total": p.get("total", 0), "task_id": task_id})
                # status/ask/done/error 不重放：终态与 waiting 补发由下方逻辑负责，避免与前端状态机重复
        else:
            for s in storage.list_steps(task_id):
                if s["llm_thought"]:
                    yield _sse(None, "thought", {"turn": s["turn_index"], "step": s["step_index"], "text": s["llm_thought"], "task_id": task_id})
                if s["tool_name"]:
                    yield _sse(
                        None,
                        "tool_call",
                        {"turn": s["turn_index"], "step": s["step_index"], "name": s["tool_name"], "arguments": s["tool_arguments"], "task_id": task_id},
                    )
                    tr = s["tool_result"] or {}
                    yield _sse(
                        None,
                        "tool_result",
                        {
                            "turn": s["turn_index"],
                            "step": s["step_index"],
                            "ok": tr.get("ok"),
                            "content_summary": (tr.get("content") or "")[:500],
                            "is_denied": tr.get("is_denied", False),
                            "task_id": task_id,
                        },
                    )

    # ---- 2. 任务已结束 → 补发终态事件 ----
    task = storage.get_task(task_id)  # 重读，取最新状态
    if task.state == TaskState.DONE:
        # 2026-09-26：重放过 done 则只发 status(done) 作收尾信号（回答已逐轮渲染），否则补 done
        if replayed_done:
            yield _sse(None, "status", {"state": "done", "turn": task.turn, "step": task.step})
        else:
            yield _sse(None, "done", {"result": task.result})
        return
    if task.state == TaskState.FAILED:
        yield _sse(None, "error", {"message": task.error or "任务失败", "recoverable": False})
        return
    if task.state == TaskState.PAUSED:
        yield _sse(None, "status", {"state": "paused", "turn": task.turn, "step": task.step})
        return
    if task.state == TaskState.WAITING:
        # S3.3：挂起中 → 补发当前状态与未回答的问题，然后继续订阅实时
        yield _sse(None, "status", {"state": "waiting", "turn": task.turn, "step": task.step})
        if run is not None and run.pending_question():
            yield _sse(None, "ask", {"type": "clarify", "question": run.pending_question()})

    # ---- 3. 仍在运行 → 订阅实时事件，直到 done/error ----
    if run is None:
        # 异常场景（任务在跑但没有运行记录）：等不到事件，直接结束
        yield _sse(None, "status", {"state": task.state.value, "turn": task.turn, "step": task.step})
        return

    q = run.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(asyncio.to_thread(q.get), timeout=15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # 心跳保活（契约 §3.5）
                continue
            yield _sse(ev["id"], ev["type"], ev["data"])
            if ev["type"] in ("done", "error"):
                break
    finally:
        run.unsubscribe(q)
