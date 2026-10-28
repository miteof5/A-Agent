# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase: Python 3.12 + FastAPI (port 8000) + OpenAI-compatible LLM client; front-end is a single-file HTML app (vanilla JS + EventSource/SSE) with local vendored libs (marked + DOMPurify), zero CDN, zero external API. New UI ships as a new single-file HTML in the project root, keeping the same backend contract.

## Users

Primary user: the developer (owner) running ActionAgent locally on Windows. Job: observe the agent executing tasks, approve or deny high-risk shell/file operations, review thinking/tool-call traces, manage sessions, logs, memory and model configuration — from one console. No other audiences confirmed.

## Product Purpose

ActionAgent is a locally-run autonomous AI agent: it receives natural-language tasks, plans autonomously, executes real actions on the computer (shell/file/network), self-heals on errors, and completes goals. The web UI is its operations console — the primary surface for supervising, steering, and debugging the agent.

## Positioning

Hand-written ReAct main loop with a plugin micro-kernel (EventBus + BasePlugin), local SQLite event sourcing and FTS5 long-term memory, permission sandbox, model hot-switching. Its defensible core is real computer-operation execution (shell/file) with full observability — not a chat wrapper.

## Operating Context

Windows localhost; FastAPI backend at :8000; SSE streaming for tokens and status polling; SOCKS5 proxy support (v2rayN/Clash) for network tools; SQLite for events/memory; model registry with hot switch (aliyun bailian compatible API as default); frontend renders Markdown locally with marked + DOMPurify. Uses: task execution, approval flows, log inspection, model switching, memory review.

## Capabilities and Constraints

- Confirmed features to surface: conversation, thinking/reasoning trace (collapsible), permission approval sandbox, Q&A/answers panel, logs, model management (hot switch), token usage display, day/night theme adaptation, long-term memory (FTS5), rolling memory compression, task lifecycle (create/status/stop/respond/continue/delete).
- Confirmed backend contract (src/api.py): POST /api/v1/task/create, GET /api/v1/task/status, POST /api/v1/task/stop, POST /api/v1/task/respond, GET /api/v1/tasks, DELETE /api/v1/tasks/{task_id}, POST /api/v1/task/{task_id}/continue, GET /api/v1/task/logs, GET /api/v1/task/stream (SSE), GET /api/v1/models, POST /api/v1/models/switch.
- Technical constraints: Python 3.12; frontend must stay single-file, zero CDN (offline-capable), keep marked/purify vendored; responsive to desktop usage; night/day theme driven by solar algorithm in the incumbent, new design may choose its own theming but must keep a day/night capability.
- Open/undecided: none material for this surface.

## Brand Commitments

Product name: ActionAgent. No external brand, logo, or voice commitments confirmed. Owner's stated preference: avoid generic "AI-slop" design; the UI should look professionally crafted, not template-like.

## Evidence on Hand

- README.md (feature list, tech stack, quick start)
- src/static/index.html (incumbent UI, 59KB single file)
- src/api.py (routes listed above)
- Project planning docs: 项目规划-v0.3.md, 接口契约-S0.md, 项目状态-交接文档.md
- No screenshots/goldens committed; incumbent visual is the only reference.

## Product Principles

1. **Local-first:** everything runs locally; the frontend must not depend on CDN or external services.
2. **Observability:** every step of a task (thinking, tool calls, approvals) is visible and inspectable.
3. **Safety boundary:** high-risk operations go through an explicit approval sandbox; the UI must make the risk clear before approval.
4. **Operate over decorate:** this is an operations console — scanability, states, and real usage scenes outrank expression; brand lives in precise details.
5. **Single-file, maintainable:** keep the frontend one HTML file with vendored deps, so the owner can diff and maintain it by hand.

## Accessibility & Inclusion

No product-specific standard confirmed (personal tool). Design should still ship with keyboard focus, sufficient contrast, and non-decorative motion. Light/dark both supported.
