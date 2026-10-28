## Direction contract

THESIS: The ActionAgent console reads as a technical drawing being rolled out: task execution, tool calls, approvals, and status all speak drafting language, and it refuses the generic SaaS recipe of cards, gradients, and rounded corners. Its unique mechanism is a self-driving agent whose every real action on the machine is observable, steerable, and auditable.

OWN-WORLD: Drawing paper (warm white by day, deep blue-graphite at night) with an ink hierarchy — hairline structure rules, 1px regular lines, 2px primary outlines; engineering blue (#1e56c4) as the sole interactive accent; drafting state colors (green = passed, red = rejected/error, amber = pending approval/warning). All data and annotations set in monospace (Consolas/SF Mono) at small sizes; tool calls render as numbered part-callouts with leader lines; approvals render as drawing review stamps (APPROVED/DENIED stamped with rotation).

STORY: Opening the page is like unrolling a drawing sheet: the title block gives task status, model, and token budget; the left rail is the drawing register (session list); the central flow reads like a construction log where every tool call is a numbered part with its parameters and result annotated; an approval is a review stamp on the sheet. Everything can be inspected — nothing is hidden.

FIRST VIEWPORT: Top title block bar (brand · task title · state cell · model · token cells · theme). Left rail: 新建任务 button + session register with state dots. Center: conversation flow with an example task (user bubble, collapsed thinking process, numbered tool call, pending approval card). Right: collapsible panel with 日志 / 模型 tabs. Bottom: drafting-table input row (mode select + send). Primary action: the bottom input.

FORM: engineering-drawing / blueprint world — ASSIGNED INDEX 4 of the grounded list, seed key 5ea0c658.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
