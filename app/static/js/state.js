/** 单一状态对象 + 手写发布订阅。
 *
 * 不用 Proxy 做响应式：原生手写深度代理边界情况多（数组、新增键、性能），
 * 这里是回合制游戏，状态变更点很集中，显式 notify() 反而更可控。
 */

export const state = {
  booted: false,
  busy: false,
  player: null,
  meta: null,          // 图标与静态配置
  runId: null,
  hp: 0, hpMax: 0,
  infection: 0, infectionBand: "",
  noise: 0, horde: false,
  flashlight: null,
  depth: 1, maxDepth: 5,
  regionProgress: 0, regionUnlocked: 1,  // 已通关地区 / 已解锁地区
  turn: 0, kills: 0, score: 0,
  evacCountdown: null,
  weapon: null,
  armor: null,
  armorDesc: null,
  ammo: {},
  cash: 0,
  scrap: 0,
  tape: 0,
  inventory: [],
  merchant: null,
  repairOptions: [],
  repairRates: null,
  room: null,
  exits: [],
  inCombat: false,
  enemies: [],
  bossAlive: false,
  level: null,
  actions: [],
  status: "active",
  epitaph: null,
  deathCause: null,
  legacyChoices: [],
  legacyBlocked: [],
  talentOptions: null,
  talent: null,
  pendingDecision: null,
  aiDegraded: false,
  icons: {},
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) fn(state);
}

/** 改状态后显式通知。所有变更都走这里，便于将来加日志/时间旅行调试。 */
export function mutate(fn) {
  fn(state);
  notify();
}

/** 把服务端下发的完整状态覆盖到本地。
 *  不做乐观预测——回合制没有实时性要求，服务端才是权威。 */
export function applyServerState(data) {
  mutate((s) => {
    if (!data) return;
    const st = data.state || {};
    Object.assign(s, {
      runId: data.run_id ?? s.runId,
      hp: st.hp ?? s.hp,
      hpMax: st.hp_max ?? s.hpMax,
      stamina: st.stamina ?? s.stamina,
      staminaMax: st.stamina_max ?? s.staminaMax,
      buffs: st.buffs ?? s.buffs,
      infection: st.infection ?? s.infection,
      infectionBand: st.infection_band ?? "",
      noise: st.noise ?? 0,
      horde: !!st.horde,
      flashlight: st.flashlight ?? null,
      depth: st.depth ?? s.depth,
      maxDepth: st.max_depth ?? s.maxDepth,
      turn: st.turn ?? s.turn,
      kills: st.kills ?? s.kills,
      score: st.score ?? s.score,
      evacCountdown: st.evac_countdown ?? null,
      weapon: st.weapon ?? s.weapon,
      armor: st.armor ?? s.armor,
      armorDesc: st.armor_desc ?? s.armorDesc,
      backpack: st.backpack ?? s.backpack,
      bagCap: st.bag_cap ?? s.bagCap,
      bagUsed: st.bag_used ?? s.bagUsed,
      ammo: st.ammo ?? s.ammo,
      cash: st.cash ?? s.cash,
      scrap: st.scrap ?? s.scrap,
      tape: st.tape ?? s.tape,
      inventory: st.inventory ?? s.inventory,
      merchant: st.merchant ?? s.merchant,
      npc: st.room?.npc ?? s.npc,
      repairOptions: st.repair_options ?? s.repairOptions,
      repairRates: st.repair_rates ?? s.repairRates,
      room: st.room ?? s.room,
      exits: st.exits ?? s.exits,
      inCombat: !!st.in_combat,
      enemies: st.enemies ?? [],
      bossAlive: !!st.boss_alive,
      level: st.level ?? s.level,
      status: st.status ?? s.status,
    });
    s.actions = data.available_actions ?? [];
    s.epitaph = data.epitaph ?? null;
    s.deathCause = data.death_cause ?? null;
    s.legacyChoices = data.legacy_choices ?? [];
    s.legacyBlocked = data.legacy_blocked ?? [];
    s.talentOptions = data.talent_options ?? null;
    s.talent = data.talent ?? s.talent;
    s.pendingDecision = data.pending_decision ?? null;
    s.aiDegraded = !!data.ai_degraded;
    if (data.icons) Object.assign(s.icons, data.icons);
  });
}
