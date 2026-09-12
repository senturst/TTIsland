/** 渲染层：分区重渲染，互不干扰。 */

import { state } from "./state.js";

/* UI 动作图标。游戏数据类的图标（物品/怪物/层）由服务端下发，不写死在这里。 */
const ACTION_ICONS = {
  attack: "⚔️", shoot: "🔫", flee: "🏃", search: "🔍", descend: "⬇️",
  evac: "🚁", use: "🧪", equip: "🎽", campfire: "🔥", move: "➜",
  status: "📊", give_up: "🏳️", lure: "📢", talent: "🧬", legacy: "🎁",
  zombify: "☣️", end: "🕯️", merchant: "🛒", discard: "🗑️",
  hazard: "☢️", npc: "🧍", repair: "🔧",
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

/* ------------------------------------------------------------------ */
/* 「放弃这一局」防误触状态（模块级，跨重渲染保留）。
 * giveUpArmed=true 表示已按过一次（V 或点击），等待第二次点击确认。
 * armGiveUp(false) 由超时/取消路径调用，解除确认态。 */
export let giveUpArmed = false;
let giveUpTimer = null;

export function armGiveUp(on) {
  giveUpArmed = !!on;
  clearTimeout(giveUpTimer);
  if (giveUpArmed) {
    giveUpTimer = setTimeout(() => {
      giveUpArmed = false;
      renderCommandsLocal();  // 超时解除：把按钮文案恢复成「放弃这一局」
    }, 3000);
  }
}

/** renderAll 传入的 onAction 闭包，供确认态就地重绘时复用。 */
let lastOnAction = null;

/** 就地重绘命令区（仅 give_up 确认态切换用）。armGiveUp 定义在后面，前置声明。 */
function renderCommandsLocal() {
  if (lastOnAction) renderCommands(lastOnAction);
}

/** ASCII 进度条。文字游戏用字符画血条，零依赖且契合终端质感。 */
function asciiBar(ratio, width = 12) {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function iconFor(id) {
  // meta.icons 的值是 {name, icon} 对象（不是字符串）——取 .icon，
  // 否则模板字符串里会渲染成 "[object Object] 砍刀"
  const v = state.icons?.[id];
  if (!v) return "";
  return typeof v === "string" ? v : (v.icon || "");
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

  // 层（带当前层所属地区名：从 meta.regions 的 levels 反查）
  const lvlIcon = iconFor("_level") || "📍";
  let region = null;
  for (const r of Object.values(s.meta?.regions || {})) {
    if ((r.levels || []).includes(s.depth)) { region = r; break; }
  }
  const regionTag = region ? `${region.icon || ""} ${region.name} · ` : "";
  setHTML("c-depth", `${iconOf(lvlIcon)} ${regionTag}第 ${s.depth} / ${s.maxDepth} 层`);

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

  // 修理换算（服务端下发）：每次点击消耗多少资源、可修几点（1 废料修 3 点）
  const rates = state.repairRates || {};
  const scrapCost = rates.scrap_cost ?? 1;
  const scrapPts = rates.scrap_points ?? 1;
  const scrapQty = state.scrap ?? 0;

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
    ic.textContent = iconFor(it.id) || (
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

    // 背包里的物品：丢弃按钮（带二次确认，明确告知无法找回）
    if (!it.held && !locked) {
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn btn-danger pack-drop";
      drop.textContent = "丢弃";
      drop.disabled = state.busy;
      drop.title = "丢弃后无法找回";
      drop.addEventListener("click", (e) => {
        e.stopPropagation();
        if (drop.dataset.confirm !== "1") {
          // 第一次点：变成确认态，3 秒内再点才真正丢弃
          drop.dataset.confirm = "1";
          drop.textContent = "确认丢弃？";
          drop.title = "丢弃后无法找回！再点一次确认";
          setTimeout(() => {
            if (drop.isConnected) {
              drop.dataset.confirm = "";
              drop.textContent = "丢弃";
            }
          }, 3000);
          return;
        }
        onAction({ id: "discard", choice: "drop", item: it.id });
      });
      node.appendChild(drop);
    }

    // 残血武器（手上的或背包里的）：废料修理——非商人区域也能修
    const maxd = maxWearOf(it);
    const repairable = !locked && it.kind === "weapon" && it.wear != null
      && it.wear < maxd && !state.inCombat && scrapQty >= scrapCost;
    if (repairable) {
      const fix = document.createElement("button");
      fix.type = "button";
      fix.className = "btn btn-safe pack-fix";
      fix.textContent = `🧱${scrapCost} 修${scrapPts}点`;
      fix.disabled = state.busy;
      fix.title = `用 ${scrapCost} 废料修 ${scrapPts} 点耐久（余数舍弃，可逐次修满，不降耐久上限）`;
      fix.addEventListener("click", (e) => {
        e.stopPropagation();
        onAction({ id: "repair", item: it.id, pay: "scrap" });
      });
      node.appendChild(fix);
    }

    box.appendChild(node);
  }
}

/** 物品的耐久上限：手上的武器/护甲取配置耐久（服务端 repairOptions.max），
 *  背包装备同样从 repairOptions 找；找不到时返回 Infinity（视为满耐久，不显示修按钮）。 */
function maxWearOf(it) {
  const opts = state.repairOptions || [];
  const r = opts.find((o) => o.id === it.id);
  return r ? r.max : Infinity;
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
    // 确认态恢复：重渲染后按钮是新的，但「放弃这一局」的确认意图应保留——
    // 否则玩家第一次按了 V（进确认态）后任何响应回来，按钮刷新确认就丢了
    if (a.id === "give_up" && giveUpArmed) {
      label.textContent = " 确认放弃？（再点一次）";
      btn.classList.add("btn-confirm-giveup");
    } else {
      label.textContent = " " + a.label;
    }

    btn.append(ic, label);

    const hk = hotkeyForAction(index);
    if (hk) {
      const k = document.createElement("kbd");
      k.className = "hot";
      k.textContent = hk;
      btn.appendChild(k);
    }

    // 放弃当局：防误触。第一次点击只进入确认态（3 秒超时自动解除），
    // 第二次点击才真正执行。快捷键（V）只负责进入确认态——
    // 真正的放弃必须用鼠标点第二下，键盘连按永远不会误杀当局。
    if (a.id === "give_up") {
      btn.addEventListener("click", () => {
        if (!giveUpArmed) {
          armGiveUp(true);
          renderCommandsLocal();  // 立即重绘确认态文案，不发请求
          return;
        }
        armGiveUp(false);
        onAction(a);
      });
    } else {
      btn.addEventListener("click", () => onAction(a));
    }
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

  // 「离开」常驻面板头部（sticky）：商品再多也能随时撤，绝不依赖被挤出屏幕的命令区
  const leaveBtn = document.getElementById("merchant-leave");
  if (leaveBtn) {
    leaveBtn.disabled = state.busy;
    leaveBtn.onclick = () => onAction({ id: "merchant", choice: "leave" });
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
      const soldOut = !!s.sold;
      const priceNote = soldOut
        ? `已售出`
        : fullPrice
          ? `原价 💰 ${s.value}（无折扣）`
          : `💰 ${s.cost}${s.value !== s.cost ? `（值 ${s.value}）` : ""}`;
      card.innerHTML =
        `<span class="mch-name">${iconFor(s.id) || "📦"} ${s.name}</span>` +
        `<span class="mch-desc">${s.desc || ""}</span>` +
        `<span class="mch-cost">${priceNote}</span>`;
      if (mercyItem && !soldOut) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-primary mch-btn";
        b.textContent = "免费拿（怜悯）";
        b.disabled = state.busy;
        b.addEventListener("click", () => onAction({ id: "merchant", choice: "mercy", item: s.id }));
        card.appendChild(b);
      } else if (soldOut) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-ghost mch-btn";
        b.textContent = "已售出";
        b.disabled = true;
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

  // ---- 修理：每件可修装备给废料 / 现金两个按钮（每次消耗 1 份资源修多点，可逐次修满）----
  const repBox = document.getElementById("merchant-repair");
  if (repBox) {
    repBox.innerHTML = "";
    const opts = state.repairOptions || [];
    const rates = state.repairRates || {};
    const rateNote = rates.scrap_cost
      ? `修 1 次：🧱 ${rates.scrap_cost} 修 ${rates.scrap_points} 点，或 💰 ${rates.cash_cost} 修 ${rates.cash_points} 点（余数舍弃）`
      : "";
    if (!opts.length) {
      repBox.innerHTML = `<span class="pack-empty">没有要修的</span>`;
    }
    for (const r of opts) {
      const card = document.createElement("div");
      card.className = "mch-card";
      const canScrap = scrap >= r.scrap_cost;
      const canCash = cash >= r.cash_cost;
      const sp = Math.min(r.scrap_points ?? 1, r.max - r.cur);
      const cp = Math.min(r.cash_points ?? 1, r.max - r.cur);
      card.innerHTML =
        `<span class="mch-name">${iconFor(r.id) || "🔧"} ${r.name}</span>` +
        `<span class="mch-desc">耐久 ${r.cur}/${r.max}${rateNote ? ` · ${rateNote}` : ""}</span>`;
      const bs = document.createElement("button");
      bs.type = "button";
      bs.className = "btn btn-safe mch-btn";
      bs.textContent = `废料修 ${sp} 点（🧱 ${r.scrap_cost}）`;
      bs.disabled = !canScrap || state.busy;
      bs.title = scrap < r.scrap_cost ? "废料不够" : "每次消耗 1 废料修 3 点耐久（余数舍弃），可逐次修满；不降耐久上限";
      bs.addEventListener("click", () => onAction({ id: "merchant", choice: "repair", item: r.id, pay: "scrap" }));
      const bc = document.createElement("button");
      bc.type = "button";
      bc.className = "btn btn-safe mch-btn";
      bc.textContent = `现金修 ${cp} 点（💰 ${r.cash_cost}）`;
      bc.disabled = !canCash || state.busy;
      bc.title = cash < r.cash_cost ? "现金不够" : "每次消耗 1 现金修 2 点耐久（余数舍弃），可逐次修满；不降耐久上限";
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
      // 每次只卖 1 件；该商人不再收购已成交过的同款（防折价买→回收卖套利）
      card.innerHTML =
        `<span class="mch-name">${iconFor(i.id) || "📦"} ${i.name}${i.qty > 1 ? ` ×${i.qty}` : ""}</span>` +
        `<span class="mch-desc">回收 💰 ${i.sell}（卖 1 件）</span>`;
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
/** 幸存者面板（P6.2.2）：废料换物。分享/离开走命令区按钮，这里只放货架。 */
export function renderNpc(onAction) {
  const box = document.getElementById("npc");
  if (!box) return;

  const npc = state.npc;
  const inRoom = state.room?.type === "special" && !!npc && !state.room?.resolved;
  show("npc", !!npc && inRoom);
  if (!npc || !inRoom) return;

  const leaveBtn = document.getElementById("npc-leave");
  if (leaveBtn) {
    leaveBtn.disabled = state.busy;
    leaveBtn.onclick = () => onAction({ id: "npc", choice: "leave" });
  }

  const stockBox = document.getElementById("npc-stock");
  if (!stockBox) return;
  stockBox.innerHTML = "";
  const scrap = state.scrap ?? 0;

  if (!npc.stock.length) {
    stockBox.innerHTML = `<span class="pack-empty">他没什么可换的</span>`;
    return;
  }
  for (const s of npc.stock) {
    const card = document.createElement("div");
    card.className = "mch-card";
    const soldOut = !!s.sold;
    card.innerHTML =
      `<span class="mch-name">${iconFor(s.id) || "📦"} ${s.name}</span>` +
      `<span class="mch-desc">${s.desc || ""}</span>` +
      `<span class="mch-cost">🧱 ${s.cost}</span>`;
    const b = document.createElement("button");
    b.type = "button";
    if (soldOut) {
      b.className = "btn btn-ghost mch-btn";
      b.textContent = "已换出";
      b.disabled = true;
    } else {
      b.className = "btn btn-primary mch-btn";
      b.textContent = "用废料换";
      b.disabled = scrap < s.cost || state.busy;
      b.title = scrap < s.cost ? "废料不够" : "";
      b.addEventListener("click", () => onAction({ id: "npc", choice: "buy", item: s.id }));
    }
    card.appendChild(b);
    stockBox.appendChild(card);
  }
}

/* ------------------------------------------------------------------ */
export function renderAll(onAction) {
  lastOnAction = onAction;
  renderHUD();
  renderEnemies();
  renderPack(onAction);
  renderMerchant(onAction);
  renderNpc(onAction);
  renderCommands(onAction);
}

export function setBusy(busy) {
  state.busy = busy;
  // 必须同时覆盖命令区与商人/幸存者面板：consume() 在 setBusy(false) 之前调用 renderAll()，
  // 面板按钮此时以 busy=true 渲染成 disabled，若这里只恢复 #cmd-buttons，
  // 买/换按钮会永远卡在灰色。
  // 随身面板的丢弃/修理小按钮同理：renderPack 在 busy=true 时渲染，
  // 漏恢复会让丢弃按钮永远点不了（玩家看起来就是"按钮被挡住了"）。
  const buttons = document.querySelectorAll(
    "#cmd-buttons .btn, #merchant .mch-btn, #npc .mch-btn, .pack-item .pack-drop, .pack-item .pack-fix"
  );
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
