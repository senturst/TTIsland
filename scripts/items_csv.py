"""物品表 CSV 导出 / 回导 / 数值审计。

用途（P8 平衡审计）：把 configs/items.yaml 的全部物品导出成 CSV，
在 Excel 里审阅修改（远程 vs 近战数值平衡），再回导回 YAML。

用法：
    python scripts/items_csv.py export  data/items_export.csv   # 导出
    python scripts/items_csv.py import  data/items_export.csv   # 回导（写回 YAML）
    python scripts/items_csv.py audit                              # 武器平衡速览

设计：
  - 回导采用**行级手术**：只改物品块内的标量行，YAML 注释与结构原样保留
    （items.yaml 里大量设计注释，整文件重写会把它们全洗掉）。
  - id 不可改（全游戏引用锚点）；CSV 里出现新 id 视为新增物品（追加到段尾，
    热重载会拒绝新增 ID，需要重启服务）；CSV 中删除的行不删除物品（仅警告）。
  - 区间字段（dmg/burst/heal/qty/infection）拆成 _min/_max 两列；
    buff（嵌套 dict）序列化为 JSON 字符串。
  - 写回前先备份 items.yaml.bak-<时间戳>；写回后立即做一次完整配置校验，
    校验失败自动回滚——坏配置绝不落盘。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

ITEMS_PATH = ROOT / "configs" / "items.yaml"

# 列顺序：section/id/name 永远在前，其余按约定顺序；未列出的标量进 extra_json
COLUMNS = [
    "section", "id", "name", "icon", "kind", "tier", "weight", "value",
    "dmg_min", "dmg_max", "acc_mod", "crit", "noise", "noise_key",
    "durability", "pry", "silent", "ammo_type", "ammo_per_shot",
    "burst_min", "burst_max", "aoe",
    "armor", "eva", "pockets", "noise_mod", "slots",
    "heal_min", "heal_max", "heal_stamina", "infection_min", "infection_max",
    "qty_min", "qty_max", "flashlight", "score",
    "rare", "desc_ai", "buff", "extra_json",
]
LIST_FIELDS = {"dmg", "burst", "heal", "qty", "infection"}  # [a, b] → _min/_max
SKIP_KEYS = {"id", "name", "icon"}  # 单列已覆盖
SECTIONS = ["weapons", "ammo", "consumables", "materials", "armor", "trinkets", "backpacks"]


def _load() -> dict:
    return yaml.safe_load(ITEMS_PATH.read_text(encoding="utf-8"))


def _flatten(section: str, it: dict) -> dict:
    row: dict[str, str] = {c: "" for c in COLUMNS}
    row["section"] = section
    extra = {}
    for k, v in it.items():
        if k in ("id", "name", "icon"):
            row[k] = str(v)
        elif k in LIST_FIELDS and isinstance(v, (list, tuple)) and len(v) == 2:
            row[f"{k}_min"], row[f"{k}_max"] = str(v[0]), str(v[1])
        elif k == "buff":
            row["buff"] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, (dict, list)):
            extra[k] = v
        elif k in COLUMNS:
            # None（如拳头 durability: null）导出为空——空 = 回导时不触碰
            row[k] = "" if v is None else str(v)
        else:
            extra[k] = v
    if extra:
        row["extra_json"] = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return row


def export_csv(out_path: str) -> None:
    data = _load()
    rows = []
    for sec in SECTIONS:
        for it in data.get(sec) or []:
            rows.append(_flatten(sec, it))
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"导出 {len(rows)} 条物品 → {out_path}")


def _scalar(v: str):
    """CSV 单元格 → YAML 标量（保持数字类型；空串/null = 删除该字段或不触碰）。"""
    v = (v or "").strip()
    if v.lower() in ("", "null", "none", "~"):
        return None, False
    low = v.lower()
    if low == "true":
        return True, True
    if low == "false":
        return False, True
    try:
        return int(v), True
    except ValueError:
        pass
    try:
        return float(v), True
    except ValueError:
        pass
    return v, True


def _pair(row: dict, key: str):
    lo, has_lo = _scalar(row.get(f"{key}_min", ""))
    hi, has_hi = _scalar(row.get(f"{key}_max", ""))
    if not (has_lo and has_hi):
        return None, False
    return [lo, hi], True


def import_csv(in_path: str) -> None:
    text = ITEMS_PATH.read_text(encoding="utf-8")
    data = _load()
    with open(in_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    # 索引现库：id → (section, item dict)
    index = {}
    for sec in SECTIONS:
        for it in data.get(sec) or []:
            index[it["id"]] = (sec, it)

    # 逐行收集目标字段值
    changed_ids, added_ids, dropped = set(), set(), []
    wanted: dict[str, dict] = {}  # id → {field: value}
    for r in rows:
        iid = (r.get("id") or "").strip()
        sec = (r.get("section") or "").strip()
        if not iid or sec not in SECTIONS:
            continue
        if iid not in index:
            added_ids.add(iid)
            dropped_note = f"（新增物品，目标段 {sec}——新增 ID 需要重启服务生效）"
        fields: dict[str, object] = {}
        extra_json = (r.get("extra_json") or "").strip()
        list_cols = set(LIST_FIELDS) | {f"{lf}_min" for lf in LIST_FIELDS} | {f"{lf}_max" for lf in LIST_FIELDS}
        for c in COLUMNS:
            if c in ("section", "id", "extra_json") or c in list_cols:
                continue
            v, has = _scalar(r.get(c, ""))
            if has:
                # buff 列是 JSON 字符串：解析回 dict 才能和 YAML 流式映射语义比较
                fields[c] = json.loads(v) if c == "buff" else v
        for lf in LIST_FIELDS:
            pair, has = _pair(r, lf)
            if has:
                fields[lf] = pair
        if extra_json:
            for k, v in json.loads(extra_json).items():
                fields[k] = v
        wanted[iid] = {"section": sec, "fields": fields}
        if iid in index:
            changed_ids.add(iid)

    for iid in index:
        if iid not in wanted and iid != "fists":
            dropped.append(iid)
    if dropped:
        print(f"!! 警告：CSV 中缺少这些现有物品（不会删除，请确认是否有意）：{dropped}")

    # ---- 行级手术：逐字段更新物品块的标量行 ----
    lines = text.splitlines(keepends=True)
    # 定位每个物品块：段标题行 → 段内 "  - id: X" 行 → 块结束（下一个同级条目或段末）
    block_spans: dict[str, tuple[int, int, int]] = {}  # id → (段起始行, id 行, 块末行)
    cur_sec = None
    cur_id = None
    cur_start = None
    for i, line in enumerate(lines):
        m_sec = None
        for sec in SECTIONS:
            if line.rstrip("\n") == f"{sec}:":
                m_sec = sec
                break
        if m_sec:
            cur_sec, cur_id, cur_start = m_sec, None, None
            continue
        if cur_sec is None:
            continue
        stripped = line.rstrip("\n")
        if stripped.startswith("  - id:"):
            if cur_id is not None:
                block_spans[cur_id] = (cur_start, id_line, i)
            cur_id = stripped.split(":", 1)[1].strip().strip('"')
            id_line = i
            cur_start = i
        elif stripped and not stripped.startswith(" ") and not stripped.startswith("  "):
            if cur_id is not None:
                block_spans[cur_id] = (cur_start, id_line, i)
            cur_sec, cur_id = None, None
    if cur_sec and cur_id:
        block_spans[cur_id] = (cur_start, id_line, len(lines))

    # 需要更新的 (id → field → new value)；缺失字段记录待插入位置
    updates: dict[int, list[tuple[str, str]]] = {}  # id_line → [(key, rendered)]
    inserts: dict[int, list[tuple[str, str]]] = {}  # id_line → 待插入字段（块内没有的键）

    def _parse_current(raw: str):
        """把 YAML 行里已有的值解析成可比对象（[a, b] 列表 / {流式映射} / 标量）。

        行尾的 ` # 注释` 先剥掉，否则带注释的行会被误判为"值变了"而重写丢注释。
        """
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1]  # YAML 引号字符串 → 去引号比较
        else:
            raw = raw.split(" #", 1)[0].strip()  # 剥行尾注释（不在引号内）
        if raw.startswith("[") and raw.endswith("]") or raw.startswith("{"):
            try:
                return yaml.safe_load(raw)
            except Exception:  # noqa: BLE001
                return raw
        v, _ = _scalar(raw)
        return v

    def render(k, v):
        if k == "buff":
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (list, tuple)):
            return f"[{v[0]}, {v[1]}]"
        return str(v)

    for iid, spec in wanted.items():
        if iid not in block_spans:
            continue  # 新增物品：Phase 后续支持（当前仅报告，避免块构造复杂度）
        _, id_line, block_end = block_spans[iid]
        existing_keys = set()
        for j in range(id_line + 1, block_end):
            m = re.match(r"^\s{4,}([A-Za-z_]+)\s*:", lines[j])
            if m:
                existing_keys.add(m.group(1))
        for k, v in spec["fields"].items():
            if k in ("id",):
                continue
            if k in existing_keys:
                for j in range(id_line + 1, block_end):
                    m = re.match(r"^(\s{4,})(" + re.escape(k) + r"\s*:\s*)(.*?)\s*$", lines[j])
                    if m:
                        # 语义比较：值相同（含 +5 vs 5、[6, 10] vs [6,10]）就跳过，
                        # 不产生格式噪声 diff
                        if _parse_current(m.group(3)) != v:
                            rendered = render(k, v)
                            lines[j] = f"{m.group(1)}{k}: {rendered}\n"
                            updates.setdefault(id_line, []).append((k, rendered))
                        break
            else:
                inserts.setdefault(id_line, []).append((k, render(k, v)))

    n_field_changes = sum(len(v) for v in updates.values()) + sum(len(v) for v in inserts.values())
    if n_field_changes == 0 and not added_ids:
        print("没有需要变更的字段。")
        return

    # 从后往前插入缺失字段（贴在块内最后一个已知字段行之后）
    for id_line in sorted(inserts, reverse=True):
        if not inserts[id_line]:
            continue
        block_end = block_spans_by_line(id_line, block_spans)
        j = block_end
        for k, rendered in reversed(inserts[id_line]):
            lines.insert(j, f"    {k}: {rendered}\n")

    # 备份 + 写回
    backup = ITEMS_PATH.with_name(f"items.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copyfile(ITEMS_PATH, backup)
    ITEMS_PATH.write_text("".join(lines), encoding="utf-8")

    # 完整配置校验：失败自动回滚
    try:
        from app.data.loader import GameConfig

        GameConfig()
        print(f"回导完成：{len(changed_ids)} 件现有物品变更、{len(added_ids)} 件新增（占位未写入）。")
        print(f"备份：{backup}")
        print("同 ID 数值变更会热重载生效；若 CSV 里有新增 ID，需要重启服务。")
    except Exception as e:  # noqa: BLE001
        shutil.copyfile(backup, ITEMS_PATH)
        print(f"!! 回导后的配置校验失败，已自动回滚到备份：{e}")
        sys.exit(1)


def block_spans_by_line(id_line: int, block_spans: dict) -> int:
    for _id, (_s, il, end) in block_spans.items():
        if il == id_line:
            return end
    return id_line + 1


def audit() -> None:
    """武器平衡速览：对护甲 0/2/4/6 的期望每回合伤害（命中已计入）。"""
    data = _load()
    player_acc, player_eva = 88, 5
    rows = []
    for w in data["weapons"]:
        if w["id"] == "fists":
            continue
        acc = 88 + int(w.get("acc_mod", 0) or 0)
        hit = min(95, max(5, acc - player_eva)) / 100.0
        lo, hi = w["dmg"]
        avg = (lo + hi) / 2
        crit = float(w.get("crit", 0.02))
        volley = 1
        if w.get("burst"):
            volley = (w["burst"][0] + w["burst"][1]) / 2
        per_volley = []
        for armor in (0, 2, 4, 6):
            per_hit = max(1, avg - armor)
            crit_bonus = per_hit * (crit * (1.8 - 1))
            per_volley.append(round(hit * volley * (per_hit + crit_bonus), 1))
        rows.append((w["name"], w.get("tier", 1), "远程" if w.get("kind") == "ranged" else "近战", volley, per_volley))
    print(f"{'武器':<10}{'T':<3}{'类':<5}{'发/轮':<6}{'甲0':>7}{'甲2':>7}{'甲4':>7}{'甲6':>7}")
    for name, tier, kind, volley, pv in sorted(rows, key=lambda r: -r[4][3]):
        print(f"{name:<10}T{tier:<2}{kind:<5}{volley:<6.1f}" + "".join(f"{v:>7}" for v in pv))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "export"
    target = sys.argv[2] if len(sys.argv) > 2 else "data/items_export.csv"
    if cmd == "export":
        export_csv(target)
    elif cmd == "import":
        import_csv(target)
    elif cmd == "audit":
        audit()
    else:
        print(__doc__)
