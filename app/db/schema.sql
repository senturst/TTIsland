-- ============================================================================
-- 主库 schema
--
-- 设计要点：不建背包表、地图表、房间表。
-- 一个 run 的全部运行时状态整存整取在 runs.state_json（约 5-20KB）。
-- 一个 run 全周期约 100-300 次写，SQLite 毫无压力；
-- 换来的是省掉十几个表的 JOIN 与"改一次背包结构就要迁移"的痛苦。
-- ============================================================================

CREATE TABLE IF NOT EXISTS players (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    total_runs   INTEGER NOT NULL DEFAULT 0,
    best_depth   INTEGER NOT NULL DEFAULT 0,
    best_score   INTEGER NOT NULL DEFAULT 0,
    total_kills  INTEGER NOT NULL DEFAULT 0,
    escapes      INTEGER NOT NULL DEFAULT 0,
    humanity     INTEGER NOT NULL DEFAULT 0,
    legacy_item  TEXT,                         -- 遗物 JSON: {id, durability}
    named        INTEGER NOT NULL DEFAULT 0    -- 0=默认随机名，1=玩家自定名
);
CREATE INDEX IF NOT EXISTS idx_players_score ON players(best_score DESC);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    player_id    TEXT NOT NULL REFERENCES players(id),
    seed         INTEGER NOT NULL,
    status       TEXT NOT NULL,                -- active | escaped | dead | zombified | fled
    depth        INTEGER NOT NULL DEFAULT 1,
    turn         INTEGER NOT NULL DEFAULT 0,
    score        INTEGER NOT NULL DEFAULT 0,
    kills        INTEGER NOT NULL DEFAULT 0,
    hp           INTEGER NOT NULL DEFAULT 0,
    hp_max       INTEGER NOT NULL DEFAULT 0,
    infection    INTEGER NOT NULL DEFAULT 0,
    noise        REAL    NOT NULL DEFAULT 0,
    ammo         INTEGER NOT NULL DEFAULT 0,
    flashlight   INTEGER,
    state_json   TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    death_cause  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_player ON runs(player_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_active ON runs(status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_runs_score  ON runs(score DESC) WHERE status != 'active';

-- 死亡墓碑：P4 异步交织的数据源。
-- 首批单机闭环阶段就会写入（死亡即产生），但尚不注入他人地图。
CREATE TABLE IF NOT EXISTS graves (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    player_id    TEXT NOT NULL,
    player_name  TEXT NOT NULL,
    level        INTEGER NOT NULL,
    killer_id    TEXT,
    gear_json    TEXT,
    infection    INTEGER NOT NULL DEFAULT 0,
    epitaph      TEXT,
    is_plagued   INTEGER NOT NULL DEFAULT 0,
    claim_count  INTEGER NOT NULL DEFAULT 0,
    claim_cap    INTEGER NOT NULL DEFAULT 3,
    created_at   INTEGER NOT NULL
);
-- 世界频道。首批不启用实时推送，但表先建好——P3 接 SSE 时不用改数据层。
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    player_name TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'world',   -- world | death | system
    kind        TEXT NOT NULL DEFAULT 'chat',    -- chat | death | escape | system
    body        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_time ON chat_messages(channel, created_at DESC);

-- 私人回执：墓碑被别人摸走时通知原主人（P4）。与公开的世界播报（SSE）分开存。
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- grave_looted | system
    body        TEXT NOT NULL,
    data_json   TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_player ON notifications(player_id, read, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_graves_pick   ON graves(claim_count, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_graves_player ON graves(player_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_graves_level  ON graves(level, claim_count);
