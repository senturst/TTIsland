/** 渲染层：分区重渲染，互不干扰。 */

import { state } from "./state.js";

/* UI 动作图标。游戏数据类的图标（物品/怪物/层）由服务端下发，不写死在这里。 */
const ACTION_ICONS = {
  attack: "⚔️", shoot: "🔫", flee: "🏃", search: "🔍", descend: "⬇️",
  evac: "🚁", use: "🧪", equip: "🎽", campfire: "🔥", move: "➜",
  status: "📊", give_up: "🏳️", lure: "📢", talent: "🧬", legacy: "🎁",
  zombify: "☣️", end: "🕯️", merchant: "🛒", discard: "🗑️",
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

/** 命令区按钮的快捷键徽标。
 * 按按钮出现顺序分配 Z/X/C/V，最多前 4 个。
 * C 同时也是「使用物品」的备用键：当命令区不足 3 个按钮时生效。
 */
const HOTKEYS = ["Z", "X", "C", "V"];
function hotkeyForAction(index) {
  return HOTKEYS[index] || null;
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

  // 体力（真实资源：逃跑冲刺消耗，罐头/能量饮料/篝火回复）
  const stamMax = s.staminaMax || 20;
  const stam = s.stamina ?? 0;
  const stamRatio = stamMax ? stam / stamMax : 0;
  const stamBar = document.getElementById("stam-bar");
  if (stamBar) {
    stamBar.textContent = asciiBar(stamRatio);
    stamBar.className = "gauge-bar" + (stamRatio <= 0.25 ? " low" : stamRatio <= 0.5 ? " mid" : "");
  }
  setText("stam-num", `${stam}/${stamMax}`);

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

  // 现金 / 废铁（商人经济的两类硬通货）
  setHTML("c-cash", `💰 ${s.cash ?? 0}`);
  setHTML("c-scrap", `🧱 ${s.scrap ?? 0}`);

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

  // 天赋（悬停显示效果，防止玩家遗忘自己选了什么）
  show("c-talent", !!s.talent);
  if (s.talent) {
    setHTML("c-talent", `${iconOf(HUD_ICONS.talent)} ${s.talent.name}`);
    const tEl = document.getElementById("c-talent");
    if (tEl) tEl.title = `本局天赋：${s.talent.name}\n${s.talent.desc || ""}`;
  }

  // 当前生效的临时增益（瞄准等），让玩家看清这回合的命中加成来源
  const buffs = s.buffs || [];
  show("c-buffs", buffs.length > 0);
  if (buffs.length) {
    setHTML("c-buffs", `✨ ${buffs.map((b) => `${b.name}(${b.turns}回合)`).join(" ")}`);
  }

  setHTML("c-score", `${iconOf(HUD_ICONS.score)} ${s.score}`);

  // 背包容量（格）：单格 = 一个物品条目，可堆叠物资只占 1 格
  const cap = s.bagCap || 10;
  const used = s.bagUsed || 0;
  show("c-bag", true);
  setHTML("c-bag", `${iconOf("🎒")} 包 ${used}/${cap}`);
  const bagEl = document.getElementById("c-bag");
  if (bagEl) {
    const full = used >= cap;
    bagEl.className = "chip" + (full ? " warn" : "");
    bagEl.title = `背包 ${used}/${cap} 格`;
  }
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

  // 随身面板标题展示容量（格）：单格 = 一个物品条目
  const label = document.getElementById("pack-label");
  if (label) {
    const used = state.bagUsed || 0;
    const cap = state.bagCap || 10;
    label.textContent = `随身 ${used}/${cap}`;
  }

  const entries = [];

  // 已装备的排最前，让玩家一眼看到自己的装备状态
  if (state.weapon?.name && state.weapon.name !== "空手") {
    entries.push({
      id: "__held_weapon__", name: state.weapon.name, qty: 1,
      kind: "weapon", held: true,
      wear: state.weapon.durability != null ? state.weapon.durability : null,
      desc: state.weapon.desc || null,
    });
  }
  if (state.armor) {
    entries.push({ id: "__held_armor__", name: state.armor, qty: 1, kind: "armor", held: true, desc: state.armorDesc || null });
  }
  if (state.backpack) {
    entries.push({
      id: "__held_backpack__", name: state.backpack.name, qty: 1,
      kind: "backpack", held: true, desc: state.backpack.desc || null,
    });
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
    // 有未决决策（选天赋/遗物/尸化/捡尸/背包满等）时，随身面板一律不可点。
    // 否则死亡后点背包里任意物品，会以默认 index=-1 静默走 legacy 分支、
    // 直接跳过「留下哪件遗物」的确认——这正是要修的 bug。
    const locked = !!state.pendingDecision;
    const canAct = !it.held && (it.usable || it.wearable) && !locked;
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

    // 数值摘要：不用真的用一次，也能看到道具干了什么、数值多少
    if (it.desc) {
      const d = document.createElement("span");
      d.className = "desc";
      d.textContent = it.desc;
      node.appendChild(d);
    }

    if (canAct) {
      const actionId = it.usable ? "use" : "equip";
      node.title = it.usable ? `使用 ${it.name}` : `换上 ${it.name}`;
      // 只有命令区没有占用 C（前 3 个位置）时才显示 C 徽标
      if ((state.actions || []).length < 3) {
        const k = document.createElement("kbd");
        k.className = "hot";
        k.textContent = "C";
        node.appendChild(k);
      }
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

  (state.actions || []).forEach((a, index) => {
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

    const hk = hotkeyForAction(index);
    if (hk) {
      const k = document.createElement("kbd");
      k.className = "hot";
      k.textContent = hk;
      btn.appendChild(k);
    }

    btn.addEventListener("click", () => onAction(a));
    box.appendChild(btn);
  });
}

/* ------------------------------------------------------------------ */
/** 商人面板：买卖 / 修理 / 出售 / 作者怜悯。
 *  数据来自 state.merchant（铺货 + 折扣 + 怜悯 + 交易封锁）与 state.repairOptions。
 *  命令区只保留「离开商人」，所有交互都在这里完成。 */
export function renderMerchant(onAction) {
  const box = document.getElementById("merchant");
  if (!box) return;

  const m = state.merchant;
  const inRoom = state.room?.type === "merchant" && !state.room?.resolved;
  show("merchant", !!m && inRoom);
  if (!m || !inRoom) return;

  const title = document.getElementById("merchant-title");
  const note = document.getElementById("merchant-note");
  if (title) {
    title.textContent = m.type === "plagued" ? "感染商人" : "商人";
    title.className = "merchant-title" + (m.type === "plagued" ? " plagued" : "");
  }
  if (note) {
    const bits = [];
    if (m.discount && m.discount < 1) bits.push(`全部 ${Math.round(m.discount * 100)}% 折扣`);
    if (m.authors_mercy && !m.mercy_taken) bits.push("今天他破例送你一件");
    if (m.full_price) bits.push("⚠ 血量不过半：无折扣按原价交易，不抽血");
    else if (m.type === "plagued") bits.push("成交时你缺多少血它抽多少");
    note.textContent = bits.join(" · ");
  }

  const fullPrice = !!m.full_price;
  const cash = state.cash ?? 0;
  const scrap = state.scrap ?? 0;

  // ---- 货架：买 / 作者怜悯 ----
  const shopBox = document.getElementById("merchant-shop");
  if (shopBox) {
    shopBox.innerHTML = "";
    if (!m.shop.length) {
      shopBox.innerHTML = `<span class="pack-empty">空空如也</span>`;
    }
    for (const s of m.shop) {
      const card = document.createElement("div");
      card.className = "mch-card";
      // full_price 时按原价（value）出售；正常显示折扣价 cost
      const price = fullPrice ? s.value : s.cost;
      const canAfford = cash >= price;
      const mercyItem = m.authors_mercy && !m.mercy_taken;
      const priceNote = fullPrice
        ? `原价 💰 ${s.value}（无折扣）`
        : `💰 ${s.cost}${s.value !== s.cost ? `（值 ${s.value}）` : ""}`;
      card.innerHTML =
        `<span class="mch-name">${iconFor(s.id) || "📦"} ${s.name}</span>` +
        `<span class="mch-desc">${s.desc || ""}</span>` +
        `<span class="mch-cost">${priceNote}</span>`;
      if (mercyItem) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-primary mch-btn";
        b.textContent = "免费拿（怜悯）";
        b.disabled = state.busy;
        b.addEventListener("click", () => onAction({ id: "merchant", choice: "mercy", item: s.id }));
        card.appendChild(b);
      } else {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-primary mch-btn";
        b.textContent = "购买";
        b.disabled = !canAfford || state.busy;
        b.title = cash < price ? "现金不够" : (fullPrice ? "血量不过半，按原价交易" : "");
        b.addEventListener("click", () => onAction({ id: "merchant", choice: "buy", item: s.id }));
        card.appendChild(b);
      }
      shopBox.appendChild(card);
    }
  }

  // ---- 修理：每件可修装备给废铁 / 现金两个按钮 ----
  const repBox = document.getElementById("merchant-repair");
  if (repBox) {
    repBox.innerHTML = "";
    const opts = state.repairOptions || [];
    if (!opts.length) {
      repBox.innerHTML = `<span class="pack-empty">没有要修的</span>`;
    }
    for (const r of opts) {
      const card = document.createElement("div");
      card.className = "mch-card";
      const canScrap = scrap >= r.scrap_cost;
      const canCash = cash >= r.cash_cost;
      card.innerHTML =
        `<span class="mch-name">${iconFor(r.id) || "🔧"} ${r.name}</span>` +
        `<span class="mch-desc">耐久 ${r.cur}/${r.max}</span>`;
      const bs = document.createElement("button");
      bs.type = "button";
      bs.className = "btn btn-safe mch-btn";
      bs.textContent = `废铁修（🧱 ${r.scrap_cost}）`;
      bs.disabled = !canScrap || state.busy;
      bs.title = scrap < r.scrap_cost ? "废铁不够" : "每次修理会降低耐久上限";
      bs.addEventListener("click", () => onAction({ id: "merchant", choice: "repair", item: r.id, pay: "scrap" }));
      const bc = document.createElement("button");
      bc.type = "button";
      bc.className = "btn btn-safe mch-btn";
      bc.textContent = `现金修（💰 ${r.cash_cost}）`;
      bc.disabled = !canCash || state.busy;
      bc.title = cash < r.cash_cost ? "现金不够" : "每次修理会降低耐久上限";
      bc.addEventListener("click", () => onAction({ id: "merchant", choice: "repair", item: r.id, pay: "cash" }));
      card.append(bs, bc);
      repBox.appendChild(card);
    }
  }

  // ---- 出售：背包里可卖的道具 ----
  const sellBox = document.getElementById("merchant-sell");
  if (sellBox) {
    sellBox.innerHTML = "";
    const sellable = (state.inventory || []).filter((i) => i.qty > 0 && i.sell != null);
    if (!sellable.length) {
      sellBox.innerHTML = `<span class="pack-empty">没东西可卖</span>`;
    }
    for (const i of sellable) {
      const card = document.createElement("div");
      card.className = "mch-card";
      const total = (i.sell || 0) * i.qty;
      card.innerHTML =
        `<span class="mch-name">${iconFor(i.id) || "📦"} ${i.name}${i.qty > 1 ? ` ×${i.qty}` : ""}</span>` +
        `<span class="mch-desc">回收 💰 ${total}</span>`;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-ghost mch-btn";
      b.textContent = "出售";
      b.disabled = state.busy;
      b.title = "";
      b.addEventListener("click", () => onAction({ id: "merchant", choice: "sell", item: i.id }));
      card.appendChild(b);
      sellBox.appendChild(card);
    }
  }
}

/* ------------------------------------------------------------------ */
export function renderAll(onAction) {
  renderHUD();
  renderEnemies();
  renderPack(onAction);
  renderMerchant(onAction);
  renderCommands(onAction);
}

export function setBusy(busy) {
  state.busy = busy;
  const buttons = document.querySelectorAll("#cmd-buttons .btn");
  buttons.forEach((b) => { b.disabled = busy; });
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

/* ------------------------------------------------------------------ */
/* 世界事件播报：顶部滑入一条 + 世界面板实时列表追加。 */
const EVENT_KIND_LABEL = { death: "阵亡", escape: "撤离", record: "纪录" };

export function pushWorldEvent(ev) {
  const kind = ev.kind || "system";
  const label = EVENT_KIND_LABEL[kind] || "事件";
  const body = ev.body || "";

  // 顶部播报条
  const ticker = document.getElementById("event-ticker");
  if (ticker) {
    ticker.className = `event-ticker k-${kind}`;
    ticker.innerHTML = `<span class="et-kind">${label}</span>${body}`;
    ticker.classList.add("show");
    clearTimeout(ticker._t);
    ticker._t = setTimeout(() => ticker.classList.remove("show"), 5200);
  }

  // 世界面板实时列表
  const list = document.getElementById("world-events-list");
  if (list) {
    const empty = list.querySelector(".world-events-empty");
    if (empty) empty.remove();
    const li = document.createElement("li");
    li.innerHTML = `<span class="et-kind">${label}</span>${body}`;
    list.prepend(li);
    while (list.children.length > 30) list.lastChild.remove();
  }
}

/* ------------------------------------------------------------------ */
/* 排行榜渲染：三榜共用，by ∈ {score, depth, humanity}。 */
export function renderLeaderboard(entries, by) {
  const list = document.getElementById("world-list");
  if (!list) return;
  list.innerHTML = "";
  if (!entries.length) {
    list.innerHTML = `<li class="world-events-empty">还没有人上榜，去当第一个。</li>`;
    return;
  }
  const me = state.player?.name;
  const valKey = { score: "best_score", depth: "best_depth", humanity: "humanity" }[by] || "best_score";
  const valLabel = { score: "分", depth: "层", humanity: "人道" }[by] || "";
  entries.forEach((e, i) => {
    const li = document.createElement("li");
    if (e.name === me) li.classList.add("me");
    const val = e[valKey] ?? 0;
    li.innerHTML =
      `<span class="rank">${i + 1}</span>` +
      `<span class="name">${e.name}</span>` +
      `<span class="val">${val} ${valLabel}</span>`;
    list.appendChild(li);
  });
}
