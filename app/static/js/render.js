/** 渲染层：分区重渲染，互不干扰。 */

import { state } from "./state.js";

/* UI 动作图标。游戏数据类的图标（物品/怪物/层）由服务端下发，不写死在这里。 */
const ACTION_ICONS = {
  attack: "⚔️", shoot: "🔫", flee: "🏃", search: "🔍", descend: "⬇️",
  evac: "🚁", use: "🧪", equip: "🎽", campfire: "🔥", move: "➜",
  status: "📊", give_up: "🏳️", lure: "📢", talent: "🧬", legacy: "🎁",
  zombify: "☣️", end: "🕯️",
};

const HUD_ICONS = {
  hp: "❤️", infection: "☣️", noise: "📢", flash: "🔦",
  evac: "⏱️", talent: "🧬", score: "🏆", ammo: "🔩", armor: "🛡️",
};

export function iconOf(name) {
  return `<span class="icon">${name}</span>`;
}

function hotIcon(name) {
  return `<span class="icon hot">${name}</span>`;
}

/** ASCII 进度条。文字游戏用字符画血条，零依赖且契合终端质感。 */
function asciiBar(ratio, width = 12) {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function iconFor(id) {
  return state.icons?.[id] || "";
}

function setText(id, text) {
  const node = document.getElementById(id);
  if (node) node.textContent = text;
}

function setHTML(id, html) {
  const node = document.getElementById(id);
  if (node) node.innerHTML = html;
}

function show(id, visible) {
  const node = document.getElementById(id);
  if (node) node.classList.toggle("hidden", !visible);
}

/* ------------------------------------------------------------------ */
export function renderHUD() {
  const s = state;

  // 体力
  const ratio = s.hpMax ? s.hp / s.hpMax : 0;
  const bar = document.getElementById("hp-bar");
  if (bar) {
    bar.textContent = asciiBar(ratio);
    bar.className = "gauge-bar" + (ratio <= 0.25 ? " low" : ratio <= 0.5 ? " mid" : "");
  }
  setText("hp-num", `${Math.max(0, s.hp)}/${s.hpMax}`);

  // 感染
  const infRatio = s.infection / 100;
  const infBar = document.getElementById("inf-bar");
  if (infBar) {
    infBar.textContent = asciiBar(infRatio, 10);
    infBar.className = "gauge-bar" + (s.infection >= 75 ? " high" : "");
  }
  setText("inf-num", s.infectionBand ? `${s.infection}% ${s.infectionBand}` : `${s.infection}%`);

  // 层
  const lvlIcon = s.icons?._level || "📍";
  setHTML("c-depth", `${iconOf(lvlIcon)} 第 ${s.depth} / ${s.maxDepth} 层`);

  // 房间
  setText("c-room", s.room?.name ? `${s.room.name}` : "");

  // 武器
  const wIcon = s.weapon?.ranged ? "🔫" : "🔧";
  const wName = s.weapon?.name || "空手";
  const dur = s.weapon?.durability != null ? ` (${s.weapon.durability})` : "";
  setHTML("c-weapon", `${iconOf(wIcon)} ${wName}${dur}`);

  // 弹药
  const ammoTotal = Object.values(s.ammo || {}).reduce((a, b) => a + b, 0);
  setHTML("c-ammo", `${iconOf(HUD_ICONS.ammo)} ${ammoTotal}`);

  // 噪音
  const noiseNode = document.getElementById("c-noise");
  if (noiseNode) {
    const hot = s.noise >= 8 || s.horde;
    noiseNode.innerHTML = s.horde
      ? `${hotIcon("⚠️")} 尸潮 ${s.noise}/10`
      : `${hot ? hotIcon(HUD_ICONS.noise) : iconOf(HUD_ICONS.noise)} 噪音 ${s.noise}/10`;
    noiseNode.className = "chip" + (s.horde ? " danger" : s.noise >= 6 ? " warn" : "");
  }

  // 手电（只在黑暗层显示）
  show("c-flash", s.flashlight != null && s.depth === 2);
  if (s.flashlight != null && s.depth === 2) {
    setHTML("c-flash", `${iconOf(HUD_ICONS.flash)} ${s.flashlight}`);
  }

  // 撤离倒计时
  show("c-evac", s.evacCountdown != null);
  if (s.evacCountdown != null) {
    setHTML("c-evac", `${hotIcon(HUD_ICONS.evac)} 撤离 ${s.evacCountdown}`);
  }

  // 天赋
  show("c-talent", !!s.talent);
  if (s.talent) setHTML("c-talent", `${iconOf(HUD_ICONS.talent)} ${s.talent.name}`);

  setHTML("c-score", `${iconOf(HUD_ICONS.score)} ${s.score}`);
}

/* ------------------------------------------------------------------ */
export function renderEnemies() {
  const box = document.getElementById("enemies");
  if (!box) return;
  const alive = (state.enemies || []).filter((e) => e.hp > 0);
  show("enemies", state.inCombat && alive.length > 0);
  if (!state.inCombat || !alive.length) return;

  box.innerHTML = "";
  for (const e of alive) {
    const node = document.createElement("div");
    node.className = "enemy" + (e.hp_max >= 60 ? " boss" : "");
    const ic = document.createElement("span");
    ic.className = "icon hot";
    ic.textContent = iconFor(e.name) || "🧟";
    const name = document.createElement("span");
    name.textContent = e.name;
    const hp = document.createElement("span");
    hp.className = "hp";
    hp.textContent = `${Math.max(0, e.hp)}/${e.hp_max}`;
    node.append(ic, name, hp);
    box.appendChild(node);
  }
}

/* ------------------------------------------------------------------ */
export function renderCommands(onAction) {
  const box = document.getElementById("cmd-buttons");
  if (!box) return;
  box.innerHTML = "";

  for (const a of state.actions || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    const kindCls = a.kind ? `btn-${a.kind}` : "";
    btn.className = "btn " + kindCls + (a.id === "talent" ? " btn-talent" : "");
    btn.disabled = state.busy;

    const ic = document.createElement("span");
    ic.className = "icon" + (a.kind === "danger" ? " hot" : "");
    ic.textContent = ACTION_ICONS[a.id] || "•";

    const label = document.createElement("span");
    label.textContent = " " + a.label;

    btn.append(ic, label);
    btn.addEventListener("click", () => onAction(a));
    box.appendChild(btn);
  }
}

/* ------------------------------------------------------------------ */
export function renderAll(onAction) {
  renderHUD();
  renderEnemies();
  renderCommands(onAction);
}

export function setBusy(busy) {
  state.busy = busy;
  const buttons = document.querySelectorAll("#cmd-buttons .btn");
  buttons.forEach((b) => { b.disabled = busy; });
  const input = document.getElementById("cmd-text");
  if (input) input.disabled = busy;
}

export function flashHorde() {
  let node = document.querySelector(".flash");
  if (!node) {
    node = document.createElement("div");
    node.className = "flash";
    document.body.appendChild(node);
  }
  node.classList.add("on");
  setTimeout(() => node.classList.remove("on"), 520);
}

export function setDegraded(on) {
  const banner = document.getElementById("degraded");
  if (banner) banner.classList.toggle("hidden", !on);
}
