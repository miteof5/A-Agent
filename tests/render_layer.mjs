/* 临时验证脚本：用最小 DOM 桩跑真实的 onEvent 事件序列，统计最终 DOM 里到底有几个答案块 */
/* 渲染层回归用例（思考与答案分离 P1 的守卫）
   做法：把 index.html 里最大的内联 script 抽出来，在最小 DOM 桩里跑真实的 SSE 事件序列，
        然后数最终 DOM 里到底有几个"答案块"。
   为什么不用浏览器：这里要断言的是"同一段文本只出现一次"这种结构性事实，DOM 计数比截图可靠。
   运行：node tests/render_layer.mjs */
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync('src/static/index.html', 'utf8');
const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let js = blocks.reduce((a, b) => (a.length > b.length ? a : b));

class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = []; this.attrs = {}; this.style = {}; this._class = ''; this._text = ''; this._html = ''; this.parent = null;
  }
  get className() { return this._class; }
  set className(v) { this._class = String(v || ''); }
  get classList() {
    const self = this;
    return {
      contains: (c) => self._class.split(/\s+/).includes(c),
      add: (c) => { if (!self.classList.contains(c)) self._class = (self._class + ' ' + c).trim(); },
      remove: (c) => { self._class = self._class.split(/\s+/).filter(x => x && x !== c).join(' '); },
      toggle: (c, on) => { on ? self.classList.add(c) : self.classList.remove(c); }
    };
  }
  appendChild(c) {
    if (c.parent && c.parent !== this) { const i = c.parent.children.indexOf(c); if (i >= 0) c.parent.children.splice(i, 1); }
    c.parent = this; this.children.push(c); return c;
  }
  remove() { if (this.parent) { const i = this.parent.children.indexOf(this); if (i >= 0) this.parent.children.splice(i, 1); this.parent = null; } }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener() {}
  set innerHTML(v) {
    this._html = String(v || '');
    this.children = [];
    this._text = '';
    let cur = null;
    const re = /<(\/?)(\w+)([^>]*?)\/?>|([^<]+)/g;
    let m;
    while ((m = re.exec(this._html))) {
      if (m[4] !== undefined) {
        if (cur) cur._text += m[4]; else this._text += m[4];
      } else if (m[1] === '/') {
        cur = null;
      } else {
        const e = new El(m[2]);
        const cm = /class="([^"]*)"/.exec(m[3] || '');
        if (cm) e.className = cm[1];
        this.appendChild(e);
        cur = e;
      }
    }
  }
  get innerHTML() { return this._html; }
  get textContent() {
    if (!this.children.length && this._html && !this._text) return this._html.replace(/<[^>]*>/g, '');
    return this._text + this.children.map(c => c.textContent).join('');
  }
  set textContent(v) { this._text = String(v == null ? '' : v); this.children = []; this._html = ''; }
  scrollIntoView() {}
  focus() {}
  querySelectorAll(sel) { const out = []; walk(this, (n) => { if (n !== this && matches(n, sel)) out.push(n); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function walk(node, fn) { fn(node); node.children.forEach(c => walk(c, fn)); }
function matches(node, sel) {
  if (sel.includes(' ')) return false;
  const attr = [...sel.matchAll(/\[([\w-]+)="([^"]*)"\]/g)];
  let rest = sel.replace(/\[[^\]]*\]/g, '');
  const classes = [...rest.matchAll(/\.([\w-]+)/g)].map(m => m[1]);
  const tag = (rest.match(/^[a-zA-Z]+/) || [null])[0];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const c of classes) if (!node.classList.contains(c)) return false;
  for (const a of attr) if (node.getAttribute(a[1]) !== a[2]) return false;
  return classes.length > 0 || attr.length > 0 || !!tag;
}

const byId = {};
const document = {
  createElement: (t) => new El(t),
  getElementById: (id) => (byId[id] || (byId[id] = (() => { const e = new El('div'); e.id = id; e.value = ''; e.textContent = ''; e.options = [{ text: '—', value: 'on-demand' }]; e.selectedIndex = 0; return e; })())),
  querySelector: (s) => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: new El('body')
};
const noop = () => {};
const ctx = {
  console, document, window: { marked: null, DOMPurify: null },
  fetch: () => Promise.resolve({ ok: false, status: 500, json: async () => ({}) }),
  EventSource: class { constructor() {} addEventListener() {} close() {} },
  localStorage: { getItem: () => null, setItem: noop },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
  navigator: { clipboard: { writeText: () => Promise.resolve() } },
  location: { reload: noop }, alert: noop
};
ctx.globalThis = ctx; ctx.window.document = document; ctx.self = ctx;

// 暴露内部函数供测试调用（脚本整体包在 IIFE 里）
js = js.replace(/\}\)\(\);\s*$/, 'globalThis.__h = { onEvent, state, el, onFinished, addUserMsg, emptyChat };\n})();');
vm.createContext(ctx);
vm.runInContext(js, ctx);
const H = ctx.__h;
if (!H) throw new Error('未能暴露内部函数');

const fire = (type, payload) => H.onEvent({ type, data: JSON.stringify(payload) });
const flow = () => H.el.chatFlow;
const dump = (n, d = 0) => {
  const pad = '  '.repeat(d);
  const cls = n.className ? '.' + n.className.split(/\s+/).join('.') : '';
  const txt = n._text ? ' :: ' + JSON.stringify(n._text.slice(0, 28)) : '';
  console.log(pad + n.tagName.toLowerCase() + cls + txt);
  n.children.forEach(c => dump(c, d + 1));
};
const blocksOf = (sel) => flow().querySelectorAll(sel);

function run(name, seq, opts = {}) {
  console.log('\n' + '='.repeat(60) + '\n' + name);
  byId.chatFlow.children = [];
  H.state.liveBlock = null; H.state.reasoningBlocks = {}; H.state.lastThoughtBlock = null; H.state.lastThoughtKey = null; H.state.isReplaying = !!opts.replaying;
  H.state.userTurns = new Set();  // 模拟空会话（emptyChat 会清这个集合）
  if (opts.pre) opts.pre();
  seq.forEach(([t, p]) => fire(t, p));
  dump(flow(), 0);
  const answers = blocksOf('.msg-agent').filter(n => !n.classList.contains('live'));
  const live = blocksOf('.msg-agent').filter(n => n.classList.contains('live'));
  const procTexts = blocksOf('.text').map(n => n.textContent.trim());
  const userMsgs = blocksOf('.msg-user').map(n => n.textContent.trim());
  console.log('\n→ 答案块 %d，残留流式块 %d，过程块文本 %d 条，用户气泡 %d 条', answers.length, live.length, procTexts.length, userMsgs.length);
  return { answers, live, procTexts, userMsgs };
}

// 场景 1：真实实时序列（step0 narration→tool_call，step1 答案）
const r1 = run('实时：0 步说话+调工具，1 步直接回答', [
  ['token', { turn: 0, step: 0, delta: '我先读一下 README。' }],
  ['thought', { turn: 0, step: 0, text: '我先读一下 README。' }],
  ['tool_call', { turn: 0, step: 0, name: 'file_view', arguments: { path: 'README.md' } }],
  ['token', { turn: 0, step: 1, delta: '这个项目是 ReAct 循环实现。' }],
  ['thought', { turn: 0, step: 1, text: '这个项目是 ReAct 循环实现。' }],
  ['token', { turn: 0, step: 1, delta: '它分三层：内核、插件、UI。' }],
  ['thought', { turn: 0, step: 1, text: '这个项目是 ReAct 循环实现。它分三层：内核、插件、UI。' }],
  ['done', { result: '这个项目是 ReAct 循环实现。它分三层：内核、插件、UI。' }]
]);
const ans1 = r1.answers.map(a => a.querySelector('.md-body').textContent.trim());
console.log('断言 答案块恰 1 个 :', r1.answers.length === 1 ? 'PASS' : 'FAIL');
console.log('断言 答案文本正确   :', ans1[0] === '这个项目是 ReAct 循环实现。它分三层：内核、插件、UI。' ? 'PASS' : 'FAIL ' + JSON.stringify(ans1));
console.log('断言 过程区无同文块 :', r1.procTexts.includes('这个项目是 ReAct 循环实现。它分三层：内核、插件、UI。') ? 'FAIL' : 'PASS');
console.log('断言 第0步叙述入过程:', r1.procTexts.includes('我先读一下 README。') ? 'PASS' : 'FAIL');

// 场景 2：重放序列（只有 thought + tool_call + done，没有 token）
const r2 = run('重放：只有 thought/tool_call/done', [
  ['thought', { turn: 0, step: 0, text: '我先读一下 README。' }],
  ['tool_call', { turn: 0, step: 0, name: 'file_view', arguments: { path: 'README.md' } }],
  ['thought', { turn: 0, step: 1, text: '这个项目是 ReAct 循环实现。' }],
  ['done', { result: '这个项目是 ReAct 循环实现。' }],
  ['status', { state: 'done' }]
]);
console.log('断言 答案块恰 1 个 :', r2.answers.length === 1 ? 'PASS' : 'FAIL');
console.log('断言 过程区无同文块 :', r2.procTexts.includes('这个项目是 ReAct 循环实现。') ? 'FAIL' : 'PASS');

// 场景 3：中断（有半句话，没有 done）
const r3 = run('中断：流到一半被停止', [
  ['token', { turn: 0, step: 0, delta: '我正在处理' }],
  ['status', { state: 'paused' }]
]);
console.log('断言 半句话不算答案 :', r3.answers.length === 0 ? 'PASS' : 'FAIL');
console.log('断言 半句话收进过程区 :', r3.procTexts.includes('我正在处理') ? 'PASS' : 'FAIL');

// 场景 4：真实形态——0~2 步只吐 tool_calls（无 content），第 3 步才说话并结束
const r4 = run('多步：前几步无 narration，末步直接回答', [
  ['tool_call', { turn: 0, step: 0, name: 'file_view', arguments: { path: 'a.md' } }],
  ['tool_result', { turn: 0, step: 0, ok: true, content_summary: 'ok' }],
  ['tool_call', { turn: 0, step: 1, name: 'shell_run', arguments: { cmd: 'ls' } }],
  ['tool_result', { turn: 0, step: 1, ok: true, content_summary: 'ok' }],
  ['tool_call', { turn: 0, step: 2, name: 'web_search', arguments: { query: 'x' } }],
  ['tool_result', { turn: 0, step: 2, ok: false, error: '无结果', content_summary: '无结果' }],
  ['token', { turn: 0, step: 3, delta: '全部读完，结论如下。' }],
  ['thought', { turn: 0, step: 3, text: '全部读完，结论如下。' }],
  ['done', { result: '全部读完，结论如下。' }],
  ['status', { state: 'done' }]
]);
console.log('断言 答案块恰 1 个 :', r4.answers.length === 1 ? 'PASS' : 'FAIL');
console.log('断言 三个工具块入过程:', flow().querySelectorAll('.args').length === 3 ? 'PASS' : 'FAIL ' + flow().querySelectorAll('.args').length);

// 场景 5：同一会话第二轮（turn 1），过程块与答案块各归其位
const r5 = run('多轮：turn0 完成后再问一轮', [
  ['token', { turn: 0, step: 0, delta: '第一轮答案。' }],
  ['done', { result: '第一轮答案。' }],
  ['token', { turn: 1, step: 0, delta: '我查一下。' }],
  ['tool_call', { turn: 1, step: 0, name: 'shell_run', arguments: { cmd: 'pwd' } }],
  ['tool_result', { turn: 1, step: 0, ok: true, content_summary: 'ok' }],
  ['token', { turn: 1, step: 1, delta: '第二轮答案。' }],
  ['done', { result: '第二轮答案。' }]
]);
const boxes = flow().querySelectorAll('.proc-box');
console.log('断言 过程框只给有工具的轮 :', (boxes.length === 1 && boxes[0].getAttribute('data-turn') === '1') ? 'PASS' : 'FAIL ' + boxes.length);
console.log('断言 两个答案块     :', r5.answers.length === 2 ? 'PASS' : 'FAIL ' + r5.answers.length);
const texts = r5.answers.map(a => a.querySelector('.md-body').textContent.trim());
console.log('断言 顺序正确       :', texts[0] === '第一轮答案。' && texts[1] === '第二轮答案。' ? 'PASS' : 'FAIL ' + JSON.stringify(texts));
console.log('断言 过程区无答案   :', r5.procTexts.includes('第二轮答案。') || r5.procTexts.includes('第一轮答案。') ? 'FAIL' : 'PASS');

const fails = (globalThis.__fail = (globalThis.__fail || 0));
console.log('\n（断言结果见上方；FAIL 表示渲染层出现重复或错位）');

// ---- P2：真思维链（reasoning 通道） ----

// 场景 6：实时 + reasoning（两条通道同时在流）
const r6 = run('实时：reasoning 与 content 两条通道', [
  ['reasoning_delta', { turn: 0, step: 0, delta: '用户要读文件，' }],
  ['reasoning_delta', { turn: 0, step: 0, delta: '先 file_view。' }],
  ['content_delta', { turn: 0, step: 0, delta: '我先读一下。' }],
  ['step_end', { turn: 0, step: 0, kind: 'process', content: '我先读一下。', reasoning: '用户要读文件，先 file_view。' }],
  ['tool_call', { turn: 0, step: 0, name: 'file_view', arguments: { path: 'a.md' } }],
  ['reasoning_delta', { turn: 0, step: 1, delta: '拿到了，可以回答。' }],
  ['content_delta', { turn: 0, step: 1, delta: '文件里有两个字。' }],
  ['step_end', { turn: 0, step: 1, kind: 'answer', content: '文件里有两个字。', reasoning: '拿到了，可以回答。' }],
  ['done', { result: '文件里有两个字。' }]
]);
const rsn6 = flow().querySelectorAll('.text.rsn').map(n => n.textContent.trim());
console.log('断言 思维链条 2 段    :', rsn6.length === 2 ? 'PASS' : 'FAIL ' + rsn6.length);
console.log('断言 思维链文本正确  :', rsn6[0] === '用户要读文件，先 file_view。' && rsn6[1] === '拿到了，可以回答。' ? 'PASS' : 'FAIL ' + JSON.stringify(rsn6));
console.log('断言 答案块恰 1 个   :', r6.answers.length === 1 ? 'PASS' : 'FAIL');
console.log('断言 思维链未混入答案:', r6.answers[0].querySelector('.md-body').textContent.includes('先 file_view') ? 'FAIL' : 'PASS');
console.log('断言 思维链不进 liv块:', flow().querySelectorAll('.msg-agent .text.rsn').length === 0 ? 'PASS' : 'FAIL');

// 场景 7：重放（无 token/reasoning_delta，只有 step_end + tool_call + done）
const r7 = run('重放：step_end 驱动过程区', [
  ['step_end', { turn: 0, step: 0, kind: 'process', content: '我先读一下。', reasoning: '先 file_view。' }],
  ['tool_call', { turn: 0, step: 0, name: 'file_view', arguments: { path: 'a.md' } }],
  ['step_end', { turn: 0, step: 1, kind: 'answer', content: '文件里有两个字。', reasoning: '拿到了。' }],
  ['done', { result: '文件里有两个字。' }],
  ['status', { state: 'done' }]
], { replaying: true });
const rsn7 = flow().querySelectorAll('.text.rsn').map(n => n.textContent.trim());
console.log('断言 重放思维链 2 段 :', rsn7.length === 2 ? 'PASS' : 'FAIL ' + rsn7.length);
console.log('断言 重放答案块 1 个 :', r7.answers.length === 1 ? 'PASS' : 'FAIL ' + r7.answers.length);
console.log('断言 重放答案不重复 :', r7.procTexts.filter(t => t === '文件里有两个字。').length === 0 ? 'PASS' : 'FAIL');
console.log('断言 重放叙述入过程 :', r7.procTexts.includes('我先读一下。') ? 'PASS' : 'FAIL');

// ---- P4：用户气泡去重（"首次发送一下出现两条"回归守卫） ----
// 根因：runTask 在 createTask 返回后本地先画一条 user 气泡（乐观插入），
//       紧接着 SSE 默认 replay=true 把已落库的 agent/user 事件重放一遍，
//       前端再 addUserMsg 一次 → 同一句话上下相邻两条。
//       切换会话（openSession）走 emptyChat + 纯重放，只有一条，所以"重进就正常"。

// 场景 8：首次发送新任务 = 乐观插入 + 重放撞车 → 只该留 1 条
const r8 = run('首轮发送：乐观插入 + 重放 user 撞车', [
  ['user', { turn: 0, content: '帮我看看这个报错' }],
  ['content_delta', { turn: 0, step: 0, delta: '好的。' }],
  ['step_end', { turn: 0, step: 0, kind: 'answer', content: '好的。', reasoning: '' }],
  ['done', { result: '好的。' }]
], { replaying: true, pre: () => H.addUserMsg('帮我看看这个报错', 0) });
console.log('断言 用户气泡恰 1 条 :', r8.userMsgs.length === 1 ? 'PASS' : 'FAIL ' + r8.userMsgs.length);
console.log('断言 气泡文本无重复  :', r8.userMsgs[0] === '帮我看看这个报错' ? 'PASS' : 'FAIL ' + JSON.stringify(r8.userMsgs));

// 场景 9：切换会话纯重放两轮，内容相同也必须各留一条（去重键是轮次，不是文本）
const r9 = run('切换会话：纯重放两轮相同提问', [
  ['user', { turn: 0, content: '继续' }],
  ['done', { result: '第一答' }],
  ['user', { turn: 1, content: '继续' }],
  ['done', { result: '第二答' }],
  ['status', { state: 'done' }]
], { replaying: true });
console.log('断言 两轮各留一条    :', r9.userMsgs.length === 2 ? 'PASS' : 'FAIL ' + r9.userMsgs.length);

// 场景 10：续聊实时（replay=false，只有本地插入）→ 1 条
const r10 = run('续聊：只本地插入，无 history 重放', [
  ['content_delta', { turn: 1, step: 0, delta: '收到。' }],
  ['step_end', { turn: 1, step: 0, kind: 'answer', content: '收到。', reasoning: '' }],
  ['done', { result: '收到。' }]
], { pre: () => H.addUserMsg('继续', 1) });
console.log('断言 续聊气泡 1 条   :', r10.userMsgs.length === 1 ? 'PASS' : 'FAIL ' + r10.userMsgs.length);

// ---- P5：过程性占位文案（"加载会话历史…"/"正在执行…"）不留在对话流里 ----
// emptyChat() 的实职是"清 DOM + 重置流式块引用 + 归零 userTurns"，
// 文案只是它顺手画的提示。去掉提示后，函数仍必须被调用（否则上一轮修的重复气泡会复发）。
const r11 = run('空态：无参 emptyChat 不留占位节点', [], { pre: () => H.emptyChat() });
console.log('断言 无占位节点     :', flow().querySelectorAll('.chat-empty').length === 0 ? 'PASS' : 'FAIL ' + flow().querySelectorAll('.chat-empty').length);
console.log('断言 对话流已清空   :', flow().children.length === 0 ? 'PASS' : 'FAIL ' + flow().children.length);
console.log('断言 userTurns 归零 :', H.state.userTurns.size === 0 ? 'PASS' : 'FAIL ' + H.state.userTurns.size);
