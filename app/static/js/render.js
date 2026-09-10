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
/** 随身物品面板。
 *
 * 必须有这个面板：纪念品 / 材料 / 弹药这三类不会生成操作按钮
 * （它们没有可执行的动作），如果只靠命令区的按钮来展现物品，
 * 玩家拿到全家福这种计分道具后是完全看不见的。
 */
const KIND_ORDER = { weapon: 0, armor: 1, consumable: 2, trinket: 3, material: 4, ammo: 5 };

export function renderPack(onAction) {
  const box = document.getElementById("pack-items");
  const wrap = document.getElementById("pack");
  if (!box || !wrap) return;

  const entries = [];

  // 已装备的排最前，让玩家一眼看到自己的装备状态
  if (state.weapon?.name && state.weapon.name !== "空手") {
    entries.push({
      id: "__held_weapon__", name: state.weapon.name, qty: 1,
      kind: "weapon", held: true,
      wear: state.weapon.durability != null ? state.weapon.durability : null,
    });
  }
  if (state.armor) {
    entries.push({ id: "__held_armor__", name: state.armor, qty: 1, kind: "armor", held: true });
  }
  for (const it of state.inventory || []) {
    if (it.qty > 0) entries.push({ ...it });
  }

  entries.sort((a, b) => {
    const d = (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9);
    return d !== 0 ? d : a.name.localeCompare(b.name, "zh");
  });

  // 面板常驻显示，空了就说"空空如也"。
  // 不要用"空就隐藏"——那会让玩家分不清"没有东西"和"界面坏了"。
  box.innerHTML = "";
  if (!entries.length) {
    const none = document.createElement("span");
    none.className = "pack-empty";
    none.textContent = "空空如也";
    box.appendChild(none);
    return;
  }

  for (const it of entries) {
    const canAct = !it.held && (it.usable || it.wearable);
    const cls = ["pack-item"];
    if (it.held) cls.push("weapon-held");
    else if (canAct) cls.push("act");
    else cls.push("inert");
    if (it.kind === "trinket") cls.push("trinket");

    const node = document.createElement(canAct ? "button" : "span");
    node.className = cls.join(" ");
    if (canAct) node.type = "button";

    const ic = document.createElement("span");
    ic.className = "icon";
    ic.textContent = state.icons?.[it.id] || (
      it.kind === "trinket" ? "📦" : it.kind === "ammo" ? "🔩" :
      it.kind === "material" ? "🧱" : "•"
    );

    const nm = document.createElement("span");
    nm.textContent = it.name;

    node.append(ic, nm);

    if (it.qty > 1) {
      const q = document.createElement("span");
      q.className = "qty";
      q.textContent = `×${it.qty}`;
      node.appendChild(q);
    }
    if (it.wear != null) {
      const w = document.createElement("span");
      w.className = "wear";
      w.textContent = `·${it.wear}`;
      w.title = "剩余耐久";
      node.appendChild(w);
    }

    if (canAct) {
      const actionId = it.usable ? "use" : "equip";
      node.title = it.usable ? `使用 ${it.name}` : `换上 ${it.name}`;
      node.addEventListener("click", () =>
        onAction({ id: actionId, item: it.id, label: it.name })
      );
    }
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
  renderPack(onAction);
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
