/** 入口：装配各模块，驱动整局流程。 */

import { api } from "./api.js";
import { state, mutate, applyServerState } from "./state.js";
import { initTerm, appendLine, playLines, skipTyping, patchLine } from "./term.js";
import { renderAll, setBusy, flashHorde, setDegraded } from "./render.js";

let host = null;
let idleTimer = null;
let lastHorde = false;

/* ------------------------------------------------------------------ */
/* 信号衰减：静止 30 秒后画面变暗抖一下，任意操作恢复。 */
function resetIdle() {
  const term = document.getElementById("term");
  if (!term) return;
  term.classList.remove("degraded", "shake");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    term.classList.add("degraded", "shake");
    setTimeout(() => term.classList.remove("shake"), 200);
  }, 30000);
}

/* ------------------------------------------------------------------ */
async function handleAction(a) {
  if (state.busy) return;
  await send(a.id, {
    index: a.index,
    to: a.to,
    choice: a.choice,
    item: a.item,
  });
}

async function send(action, payload = {}) {
  if (state.busy) return;
  resetIdle();
  skipTyping();
  setBusy(true);
  try {
    const data = await api.action(action, payload);
    await consume(data);
  } catch (err) {
    appendLine(host, `! ${err.message}`, "bad");
  } finally {
    setBusy(false);
  }
}

/** 消费一次响应：播放文本 → 应用补丁 → 覆盖状态 → 重渲染。 */
async function consume(data) {
  // 先播文本，让玩家立刻看到反馈
  const lines = data.narrative || [];
  if (lines.length) await playLines(host, lines);

  // AI 风味文本到达，原地替换之前那条模板
  for (const p of data.patches || []) {
    patchLine(host, p.target, p.text);
  }

  applyServerState(data);
  renderAll(handleAction);
  setDegraded(!!data.ai_degraded);

  if (state.horde && !lastHorde) flashHorde();
  lastHorde = state.horde;

  // 结局分支。
  // 注意：服务端已经写过"你最后能留下的只有一样东西"这类引导语，
  // 这里不要再拼一条，否则会出现两句几乎一样的提示。
  if (state.pendingDecision === "legacy" && state.legacyBlocked?.length) {
    // 重火力带不走，说明原因，否则玩家会以为装备被系统吞了
    appendLine(host,
      `太重、太吵、也修不好——${state.legacyBlocked.join("、")}带不走，只能留在这里。`,
      "sys");
  }
  if (state.status !== "active" && !state.pendingDecision) {
    appendLine(host, "— 本局结束 —", "divider");
    const btn = document.getElementById("cmd-buttons");
    if (btn && !btn.querySelector(".btn-restart")) {
      const b = document.createElement("button");
      b.className = "btn btn-primary btn-restart";
      b.textContent = "再来一局";
      b.addEventListener("click", () => startRun());
      btn.appendChild(b);
    }
  }
}

/* ------------------------------------------------------------------ */
async function startRun() {
  setBusy(true);
  try {
    const data = await api.start();
    host.lines.innerHTML = "";
    state.talent = null;
    await consume(data);
  } catch (err) {
    appendLine(host, `! ${err.message}`, "bad");
  } finally {
    setBusy(false);
  }
}

/** 续玩：拉回完整画面并回放最近日志，让玩家接上上下文。 */
async function resume() {
  const data = await api.active();
  if (!data.active) return false;
  host.lines.innerHTML = "";
  for (const line of data.history || []) {
    appendLine(host, line);
  }
  appendLine(host, "— 连接恢复 —", "divider");
  applyServerState(data);
  renderAll(handleAction);
  lastHorde = state.horde;
  return true;
}

/* ------------------------------------------------------------------ */
/** 自由文本指令：把中文/英文关键字映射到 action id。 */
const KEYWORDS = [
  [/^(a|attack|攻击|打)$/, "attack"],
  [/^(s|shoot|fire|射击|开枪)$/, "shoot"],
  [/^(f|flee|run|逃跑|跑)$/, "flee"],
  [/^(search|loot|搜|翻|搜刮)$/, "search"],
  [/^(d|descend|down|下|下一层|下楼)$/, "descend"],
  [/^(evac|撤离)$/, "evac"],
  [/^(status|状态|st)$/, "status"],
  [/^(quit|giveup|放弃)$/, "give_up"],
];

function resolveText(text) {
  const t = text.trim().toLowerCase();
  for (const [re, id] of KEYWORDS) {
    if (re.test(t)) return { id, payload: {} };
  }
  // 数字：按按钮顺序执行
  if (/^\d+$/.test(t)) {
    const idx = parseInt(t, 10) - 1;
    const a = state.actions?.[idx];
    if (a) return { id: a.id, payload: { index: a.index, to: a.to, choice: a.choice, item: a.item } };
  }
  // 用 X → 使用物品
  const useMatch = t.match(/^(use|用|使用)\s*(.+)$/);
  if (useMatch) {
    const name = useMatch[2].trim();
    const hit = state.inventory.find((i) => i.name === name || i.name.includes(name));
    if (hit) return { id: "use", payload: { item: hit.id } };
  }
  return null;
}

/* ------------------------------------------------------------------ */
async function boot() {
  const statusNode = document.getElementById("boot-status");
  host = initTerm(document.getElementById("term"));

  try {
    statusNode.textContent = "校验身份…";
    const hello = await api.hello();
    mutate((s) => { s.player = hello.player; });

    statusNode.textContent = "同步配置…";
    const meta = await api.meta();
    mutate((s) => {
      s.meta = meta;
      s.maxDepth = meta.max_level;
      s.icons = { ...(meta.items || {}), ...(meta.monsters || {}) };
    });

    document.getElementById("boot").classList.add("hidden");
    document.getElementById("app").classList.remove("hidden");
    resetIdle();

    statusNode.textContent = "恢复进度…";
    const resumed = await resume();
    if (!resumed) {
      appendLine(host, "孤岛残响", "level");
      appendLine(host, "五层之下，有一架直升机。它不会等你第二次。", "flavor");
      await startRun();
    }
  } catch (err) {
    statusNode.textContent = `接入失败：${err.message}`;
    statusNode.style.color = "var(--danger)";
    return;
  }

  // 输入
  const form = document.getElementById("cmd-form");
  const input = document.getElementById("cmd-text");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    const resolved = resolveText(text);
    if (!resolved) {
      appendLine(host, `? 不明白「${text}」。用上面的按钮，或输入 攻击 / 搜刮 / 下一层。`, "sys");
      return;
    }
    await send(resolved.id, resolved.payload);
  });

  // 点击/按键跳过打字机
  document.getElementById("term").addEventListener("click", () => {
    skipTyping();
    input.focus();
  });
  document.addEventListener("keydown", (e) => {
    resetIdle();
    if (e.key === "Escape") skipTyping();
    if (e.key.length === 1 && document.activeElement !== input) input.focus();
  });

  input.focus();
}

boot();
