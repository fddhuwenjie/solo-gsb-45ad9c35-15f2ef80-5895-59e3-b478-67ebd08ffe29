"""水质异常时的「替代水源方案」核心（纯计算，不读写数据库）。

场景
----
水质异常当天，操作员必须为**既有轮灌计划**选择替代水源，但**不得改动
各分区的灌溉时刻**。系统按各候选水源的**水质状态边界**，把当日计划
切分为原子子段：每个子段内所有候选水源的状态均恒定。每个「灌溉运行组」
（同一阀门上互不重叠的每次连续供水；同阀门并发时相互重叠的分区运行
合并为一个组，共用一个水源）整体只能使用一个水源——状态边界即使落在
运行中间，也只用于核算风险/证据不足水量，不能中途换源。切换只能发生
在运行组之间的边界，且相邻两次实际供水的间隔必须满足目标水源在该阀门
上登记的**切换耗时**（间隔为 0 的相邻运行只可继续使用同一水源）。

状态边界规则与 water_trace 完全一致：
- 每条采样在采样时刻生效并持续到下一条采样（**左闭右开**）；
- 当日首条采样之前沿用当日之前最近一条历史采样的状态；
- 没有任何历史样本的前置区间为「证据不足」。

优化目标（字典序）
------------------
1. 先最小化 **风险水量（超标区间用水）+ 证据不足水量**；
2. 再最小化 **切换次数**（阀门当日首次供水源相对原绑定水源也算一次切换；
   未绑定阀门的首次供水源不计切换）；
3. 仍并列时按水源 ID 序列决胜，保证结果确定。

硬约束
------
- 水源日供水上限：当日该水源供出的总水量 ≤ daily_cap_l（None 表示不限）；
- 同源并发上限：任一时刻使用同一水源的阀门数 ≤ source_concur；
- 阀门切换间隔：同一阀门相邻运行组之间，
  间隔分钟数 ≥ 新水源在该阀门上登记的切换耗时（首个运行组不检查间隔）。

无可行解（存在无法覆盖的运行组）时，转而求「未覆盖水量最小」的方案，
并为每个未覆盖运行组列出候选水源逐一被挡的冲突原因。

水量 = 阀门额定流量 (L/min) × 子段分钟 × 该时刻阀门上同时灌溉的分区数。
"""

from datetime import datetime, timedelta

import water_quality as wq
import water_trace as wt

# 子段结论（复用用水回溯的归类）
RISK = wt.RISK                  # 超标
REVIEW = wt.REVIEW              # 观察
SAFE = wt.SAFE                  # 正常
INSUFFICIENT = wt.INSUFFICIENT  # 证据不足
# 未覆盖（无解时该运行组没有任何可供水源）
UNCOVERED = "uncovered"

CLASS_LABEL = dict(wt.CLASS_LABEL)
CLASS_LABEL[UNCOVERED] = "未覆盖"

# 计入一级目标的两类水量（风险 + 证据不足）；观察 / 正常不计
BAD_CLASSES = (RISK, INSUFFICIENT)

# DFS 访问预算：超出则返回当前找到的最好解（exhaustive=False 标注）
DEFAULT_NODE_BUDGET = 2_000_000


# ---------------------------------------------------------------- 输入校验

def parse_day(raw):
    return wt.parse_day(raw)


def validate_capacity(raw, field="日供水上限"):
    """None / '' 表示不限；否则为非负数（0 表示当日不可供水）。"""
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须为数字或留空（不限）")
    if val != val or val < 0:
        raise ValueError(f"{field}必须为非负数")
    return val


def validate_concur(raw, field="同源并发上限"):
    """同源并发上限为正整数。"""
    if isinstance(raw, bool):
        raise ValueError(f"{field}必须为正整数")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须为正整数")
    if float(val) != float(raw) or val < 1:
        raise ValueError(f"{field}必须为正整数")
    return val


def validate_switch_minutes(raw, field="切换耗时"):
    """切换耗时为非负整数分钟。"""
    if isinstance(raw, bool):
        raise ValueError(f"{field}必须为非负整数分钟")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须为非负整数分钟")
    if val < 0:
        raise ValueError(f"{field}必须为非负整数分钟")
    return val


# ---------------------------------------------------------------- 状态区间

def build_segments_by_source(samples_by_source, ranges_by_source, day):
    """{source_id: [{start,end,status,...}, ...]}，左闭右开覆盖整个当日。

    与 wt.trace_day 相同：每水源全量重算状态机，
    取当日之前最近一条采样作为历史沿用。
    """
    day_start = datetime.combine(day, datetime.min.time())
    out = {}
    for sid, raw in samples_by_source.items():
        ordered = sorted(raw, key=lambda s: (s["ts"], s["id"]))
        recomputed, _ = wq.recompute_source(
            [{k: s[k] for k in
              ("id", "source_id", "ts", "ph", "ec", "turbidity", "nitrate")}
             for s in ordered],
            ranges_by_source[sid])
        today_samples = [s for s in recomputed if s["ts"][:10] == day.isoformat()]
        carry = None
        for s in recomputed:
            if datetime.fromisoformat(s["ts"]) < day_start:
                carry = s
            else:
                break
        out[sid] = wt.status_segments(today_samples, day, carry)
    return out


def _status_at(segments, t):
    """左闭右开区间查询：t 落在哪段。"""
    for seg in segments:
        s = datetime.fromisoformat(seg["start"])
        e = datetime.fromisoformat(seg["end"])
        if s <= t < e:
            return seg
    return None


def _class_for(seg):
    return wt._STATUS_CLASS.get(seg["status"], INSUFFICIENT)


# ---------------------------------------------------------------- 运行组与子段

def _overlap_merge(intervals):
    """仅合并**严格重叠**（start < 前一 end）的区间；首尾相接不合并。

    相接的两次灌溉是两个独立运行组，组间允许满足切换耗时的换源。
    meta 保留各原始分区运行。
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: (x[0], x[1]))
    merged = []
    for start, end, meta in ordered:
        if end <= start:
            continue
        if merged and start < merged[-1][1]:
            ms, me, metas = merged[-1]
            merged[-1] = (ms, max(me, end), metas + [meta])
        else:
            merged.append((start, end, [meta]))
    return merged


def build_groups(valves, zones, items, segments_by_source,
                 candidates_by_valve, day, slot_min=15):
    """构造运行组及其原子子段。

    返回 groups: [{
        valve_id, valve_name, flow_lmin, start_dt, end_dt,
        zone_ids, pieces: [{start_dt,end_dt,minutes,multiplier,zone_ids,
                            liters, classes:{sid:klass},
                            status:{sid:{status,triggers}}}],
        bad_l:{sid: liters}, total_l, candidates:[sid]
    }]
    子段切分点 = 组内各候选水源状态边界 ∪ 各分区运行槽边界。
    """
    day_start = datetime.combine(day, datetime.min.time())
    zone_by_id = {z["id"]: z for z in zones}
    item_by_zone = {it["zone_id"]: it for it in items}
    groups = []

    for valve in sorted(valves, key=lambda v: v["id"]):
        vid = valve["id"]
        flow = float(valve["flow_lmin"])
        raw = []
        for it in items:
            zone = zone_by_id.get(it["zone_id"])
            if zone is None or zone.get("valve_id") != vid:
                continue
            s0 = day_start + timedelta(minutes=it["start_slot"] * slot_min)
            e0 = s0 + timedelta(minutes=it["length"] * slot_min)
            raw.append((s0, e0, zone))
        if not raw:
            continue

        # 候选水源：登记表为准；当前绑定但未登记的水源隐式可用（耗时 0）
        cand = {}
        for c in candidates_by_valve.get(vid, []):
            cand[c["source_id"]] = c.get("switch_minutes", 0)
        bound = valve.get("source_id")
        if bound is not None and bound not in cand:
            cand[bound] = 0
        cand = {sid: sm for sid, sm in cand.items()
                if sid in segments_by_source}

        for g_start, g_end, metas in _overlap_merge(raw):
            cuts = {g_start, g_end}
            for sid in cand:
                for seg in segments_by_source[sid]:
                    t = datetime.fromisoformat(seg["start"])
                    if g_start < t < g_end:
                        cuts.add(t)
            # 仅在「同时灌溉分区集合」发生变化的槽边界切分
            # （各重叠分区运行的起止点；单一运行内部槽边界不切）
            for z in metas:
                zit = item_by_zone.get(z["id"])
                if zit is None:
                    continue
                zs = day_start + timedelta(
                    minutes=zit["start_slot"] * slot_min)
                ze = zs + timedelta(minutes=zit["length"] * slot_min)
                for t in (zs, ze):
                    if g_start < t < g_end:
                        cuts.add(t)

            pieces = []
            for a, b in zip(sorted(cuts)[:-1], sorted(cuts)[1:]):
                if b <= a:
                    continue
                active = []
                for z in metas:
                    zit = item_by_zone.get(z["id"])
                    if zit is None:
                        continue
                    zs = day_start + timedelta(
                        minutes=zit["start_slot"] * slot_min)
                    ze = zs + timedelta(minutes=zit["length"] * slot_min)
                    if zs < b and ze > a:
                        active.append(z["id"])
                minutes = (b - a).total_seconds() / 60.0
                if minutes <= 0 or not active:
                    continue
                active = sorted(active)
                mid = a + (b - a) / 2
                classes, statuses = {}, {}
                for sid in cand:
                    seg = _status_at(segments_by_source[sid], mid)
                    if seg is None:
                        continue
                    classes[sid] = _class_for(seg)
                    statuses[sid] = {"status": seg["status"],
                                     "triggers": list(seg["triggers"])}
                pieces.append({
                    "start_dt": a, "end_dt": b,
                    "minutes": minutes,
                    "zone_ids": active,
                    "multiplier": len(active),
                    "liters": flow * minutes * len(active),
                    "classes": classes,
                    "statuses": statuses,
                })

            total_l = sum(p["liters"] for p in pieces)
            bad_l = {sid: sum(p["liters"] for p in pieces
                              if p["classes"].get(sid) in BAD_CLASSES)
                     for sid in cand}
            groups.append({
                "valve_id": vid,
                "valve_name": valve["name"],
                "flow_lmin": flow,
                "start_dt": g_start,
                "end_dt": g_end,
                "start_min": (g_start - day_start).total_seconds() / 60.0,
                "end_min": (g_end - day_start).total_seconds() / 60.0,
                "zone_ids": sorted(z["id"] for z in metas),
                "zone_names": [z["name"]
                               for z in sorted(metas, key=lambda z: z["id"])],
                "pieces": pieces,
                "total_l": total_l,
                "bad_l": bad_l,
                "candidates": sorted(cand),
            })

    groups.sort(key=lambda g: (g["start_min"], g["valve_id"]))
    return groups


# ---------------------------------------------------------------- 求解器

def _overlaps_count(a0, a1, intervals):
    return sum(1 for s, e in intervals if s < a1 and a0 < e)


def _switch_minutes_ok(prev_src, prev_end, need_minutes, this_start):
    """间隔是否满足切换耗时：首个供水（prev_src is None）不检查。"""
    if prev_src is None:
        return True
    gap = (this_start - prev_end).total_seconds() / 60.0
    return gap + 1e-9 >= need_minutes


def _solve(groups, sources, switch_by_valve, bound_by_valve,
           allow_uncovered=False, node_budget=DEFAULT_NODE_BUDGET):
    """对运行组做字典序最优水源分配（DFS）。

    groups 按开始时间全局有序，故每个阀门的前驱运行组必已赋值。
    返回 {feasible, assignments:{gi: sid|None}, bad_l, switches,
          uncovered_l, exhaustive, nodes}。
    """
    src_by_id = {s["id"]: s for s in sources}
    best = {"assignments": None, "bad_l": None, "switches": None,
            "seq": None, "uncovered_l": None}
    nodes = [0]
    # 每阀门上一实际供水组：{vid: (sid, end_dt)}
    last_by_valve = {}
    cap_used = {s["id"]: 0.0 for s in sources}
    conc_busy = {s["id"]: [] for s in sources}
    assignment = {}

    def better(bad, switches, uncovered, seq):
        if best["assignments"] is None:
            return True
        if allow_uncovered and abs(uncovered - best["uncovered_l"]) > 1e-6:
            return uncovered < best["uncovered_l"]
        if abs(bad - best["bad_l"]) > 1e-6:
            return bad < best["bad_l"]
        if switches != best["switches"]:
            return switches < best["switches"]
        return seq < best["seq"]

    def bad_lower(i):
        """剩余组差水总和下界（忽略容量/并发/切换）。

        可行模式下无候选的组直接无分支（不参与下界）；
        未覆盖模式下允许不供水，该下界不参与剪枝（调用处已区分）。
        """
        total = 0.0
        for g in groups[i:]:
            if not g["candidates"]:
                continue
            total += min(g["bad_l"].get(sid, g["total_l"])
                         for sid in g["candidates"])
        return total

    def dfs(i, bad, switches, uncovered, seq_parts):
        nodes[0] += 1
        if nodes[0] > node_budget:
            return False
        if i == len(groups):
            seq = tuple(seq_parts)
            if better(bad, switches, uncovered, seq):
                best.update(assignments=dict(assignment), bad_l=bad,
                            switches=switches, seq=seq, uncovered_l=uncovered)
            return True

        g = groups[i]
        vid = g["valve_id"]
        smap = switch_by_valve.get(vid, {})
        prev = last_by_valve.get(vid)

        if best["assignments"] is not None:
            if allow_uncovered and uncovered > best["uncovered_l"] + 1e-9:
                return True
            # 允许未覆盖时后续组可整体不供水，差水下界不成立
            if not allow_uncovered and bad + bad_lower(i) > best["bad_l"] + 1e-9:
                return True

        def opt_key(sid):
            return (g["bad_l"].get(sid, g["total_l"]), sid)

        options = sorted(g["candidates"], key=opt_key)
        if allow_uncovered:
            options.append(None)

        any_branch = False
        for sid in options:
            if sid is None:
                assignment[i] = None
                any_branch = dfs(
                    i + 1, bad, switches, uncovered + g["total_l"],
                    seq_parts + [-1]) or any_branch
                del assignment[i]
                continue

            src = src_by_id.get(sid)
            if src is None:
                continue
            cap = src.get("daily_cap_l")
            if cap is not None and cap_used[sid] + g["total_l"] > cap + 1e-6:
                continue
            if _overlaps_count(g["start_dt"], g["end_dt"],
                               conc_busy[sid]) >= int(src.get("source_concur", 1)):
                continue
            if prev is not None:
                # 继续使用同一水源无需切换，不检查间隔；换源才须满足切换耗时
                if prev[0] != sid and not _switch_minutes_ok(
                        prev[0], prev[1], smap.get(sid, 0), g["start_dt"]):
                    continue
                is_switch = 0 if prev[0] == sid else 1
            else:
                # 首次供水：未绑定阀门不计；沿用绑定水源不计
                is_switch = 0 if bound_by_valve.get(vid) in (None, sid) else 1
            add_bad = g["bad_l"].get(sid, g["total_l"])

            last_by_valve[vid] = (sid, g["end_dt"])
            cap_used[sid] += g["total_l"]
            conc_busy[sid].append((g["start_dt"], g["end_dt"]))
            assignment[i] = sid
            ok = dfs(i + 1, bad + add_bad, switches + is_switch, uncovered,
                     seq_parts + [sid])
            conc_busy[sid].pop()
            cap_used[sid] -= g["total_l"]
            if prev is None:
                del last_by_valve[vid]
            else:
                last_by_valve[vid] = prev
            del assignment[i]
            any_branch = ok or any_branch

        return any_branch

    exhaustive = dfs(0, 0.0, 0, 0.0, [])
    return {
        "feasible": best["assignments"] is not None
                    and (best["uncovered_l"] is None
                         or best["uncovered_l"] <= 1e-9),
        "assignments": best["assignments"],
        "bad_l": best["bad_l"],
        "switches": best["switches"],
        "uncovered_l": best["uncovered_l"],
        "exhaustive": exhaustive and nodes[0] <= node_budget,
        "nodes": nodes[0],
    }


# ---------------------------------------------------------------- 冲突诊断

def diagnose_group(g, gi, groups, sources, assignment, src_by_id, smap,
                   bound_by_valve):
    """无解时为单个未覆盖运行组逐候选水源给出被挡原因。"""
    vid = g["valve_id"]
    cap_used = {s["id"]: 0.0 for s in sources}
    conc_busy = {s["id"]: [] for s in sources}
    prev = None
    for j, other in enumerate(groups):
        sid = assignment.get(j)
        if sid is None or other is g:
            continue
        cap_used[sid] += other["total_l"]
        conc_busy[sid].append((other["start_dt"], other["end_dt"]))
        if other["valve_id"] == vid and other["start_dt"] < g["start_dt"]:
            if prev is None or other["start_dt"] >= prev[0]:
                prev = (other["start_dt"], other["end_dt"], sid)

    out = []
    for sid in sorted(g["candidates"],
                      key=lambda x: (x != bound_by_valve.get(vid), x)):
        src = src_by_id.get(sid)
        reasons = []
        if src is None:
            reasons.append("水源已不存在")
        else:
            cap = src.get("daily_cap_l")
            if cap is not None and cap_used[sid] + g["total_l"] > cap + 1e-6:
                reasons.append(
                    f"日供水上限不足（本组需 {_fmt(g['total_l'])} L，"
                    f"该水源已排 {_fmt(cap_used[sid])} L，上限 {_fmt(cap)} L）")
            if _overlaps_count(g["start_dt"], g["end_dt"],
                               conc_busy[sid]) >= int(src.get("source_concur", 1)):
                reasons.append(
                    f"同源并发上限 {src.get('source_concur', 1)} 已满"
                    f"（该时段已有其他阀门使用该水源）")
        if prev is not None and prev[2] != sid:
            need = smap.get(sid, 0)
            gap = (g["start_dt"] - prev[1]).total_seconds() / 60.0
            if gap + 1e-9 < need:
                reasons.append(
                    f"切换间隔不足（距上次供水结束仅 {_fmt(gap)} 分钟，"
                    f"该水源切换耗时需 {need} 分钟）")
        out.append({"source_id": sid,
                    "source_name": src["name"] if src else f"#{sid}",
                    "reasons": reasons})
    return out


def _fmt(x):
    f = float(x)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _hm(dt):
    return dt.isoformat(timespec="minutes")


# ---------------------------------------------------------------- 全日方案

def plan_day(zones, items, valves, sources, samples_by_source,
             ranges_by_source, valve_sources, day, slot_min=15):
    """计算当日替代水源方案（纯计算，不改动计划、不读写库）。

    sources:       [{id, name, daily_cap_l, source_concur}]
    valve_sources: [{valve_id, source_id, switch_minutes}]
    其余参数同 wt.trace_day。
    """
    segments = build_segments_by_source(
        samples_by_source, ranges_by_source, day)
    candidates_by_valve = {}
    switch_by_valve = {}
    for row in valve_sources:
        candidates_by_valve.setdefault(row["valve_id"], []).append(row)
        switch_by_valve.setdefault(row["valve_id"], {}) \
            [row["source_id"]] = int(row["switch_minutes"])

    groups = build_groups(
        valves, zones, items, segments, candidates_by_valve, day, slot_min)
    src_by_id = {s["id"]: s for s in sources}
    zone_by_id = {z["id"]: z for z in zones}
    bound_by_valve = {v["id"]: v.get("source_id") for v in valves}

    result = _solve(groups, sources, switch_by_valve, bound_by_valve,
                    allow_uncovered=False)
    if not result["feasible"]:
        # 允许未覆盖，最小化未覆盖水量，再最小化风险+证据不足与切换
        result = _solve(groups, sources, switch_by_valve, bound_by_valve,
                        allow_uncovered=True)
    assignment = result["assignments"] or {}
    feasible = (result["uncovered_l"] in (None, 0.0))

    # ---- 汇总 ----
    totals = {RISK: 0.0, REVIEW: 0.0, SAFE: 0.0,
              INSUFFICIENT: 0.0, UNCOVERED: 0.0}
    source_used = {s["id"]: 0.0 for s in sources}
    switches = []
    last_src = {}
    last_end = {}
    switches_by_valve = {}
    piece_rows = []
    group_rows = []

    for gi, g in enumerate(groups):
        vid = g["valve_id"]
        sid = assignment.get(gi)
        gklass = UNCOVERED if sid is None else None
        g_bad = 0.0

        # 组内子段（同一水源，但状态可能跨边界，结论逐段核算）
        for p in g["pieces"]:
            if sid is None:
                klass = UNCOVERED
                triggers = []
            else:
                klass = p["classes"].get(sid, INSUFFICIENT)
                st = p["statuses"].get(sid) or {}
                triggers = st.get("triggers", [])
            totals[klass] += p["liters"]
            piece_rows.append({
                "valve_id": vid, "valve_name": g["valve_name"],
                "zone_ids": p["zone_ids"],
                "zone_names": [zone_by_id[z]["name"] for z in p["zone_ids"]
                               if z in zone_by_id],
                "start": _hm(p["start_dt"]), "end": _hm(p["end_dt"]),
                "start_min": (p["start_dt"] -
                              datetime.combine(day, datetime.min.time()))
                    .total_seconds() / 60.0,
                "end_min": (p["end_dt"] -
                            datetime.combine(day, datetime.min.time()))
                    .total_seconds() / 60.0,
                "minutes": round(p["minutes"], 2),
                "multiplier": p["multiplier"],
                "water_l": round(p["liters"], 2),
                "source_id": sid,
                "source_name": src_by_id[sid]["name"]
                    if sid in src_by_id else None,
                "klass": klass,
                "triggers": triggers,
            })

        if sid is None:
            totals_g_l = g["total_l"]
            group_rows.append({
                "valve_id": vid, "valve_name": g["valve_name"],
                "zone_ids": g["zone_ids"], "zone_names": g["zone_names"],
                "start": _hm(g["start_dt"]), "end": _hm(g["end_dt"]),
                "water_l": round(g["total_l"], 2),
                "source_id": None, "source_name": None,
                "klass": UNCOVERED, "switched_from": None,
                "gap_minutes": None, "required_minutes": None,
            })
            continue

        source_used[sid] += g["total_l"]
        g_bad = g["bad_l"].get(sid, g["total_l"])
        piece_klasses = [p["classes"].get(sid, INSUFFICIENT)
                         for p in g["pieces"]]
        gklass = RISK if RISK in piece_klasses else \
            INSUFFICIENT if INSUFFICIENT in piece_klasses else \
            REVIEW if REVIEW in piece_klasses else SAFE
        prev_sid = last_src.get(vid)
        gap_minutes = None
        if prev_sid is None:
            if sid != bound_by_valve.get(vid):
                # 已绑定阀门当日首次供水即换源 -> 计一次切换；
                # 未绑定阀门（bound 为 None）首次确定水源不算切换
                if bound_by_valve.get(vid) is not None:
                    kind = "first"
                    switches.append({
                        "valve_id": vid, "valve_name": g["valve_name"],
                        "from_source_id": bound_by_valve.get(vid),
                        "from_source_name": src_by_id[bound_by_valve[vid]]["name"]
                            if bound_by_valve.get(vid) in src_by_id else None,
                        "to_source_id": sid,
                        "to_source_name": src_by_id[sid]["name"],
                        "at": _hm(g["start_dt"]), "at_min": g["start_min"],
                        "kind": kind, "gap_minutes": None,
                        "required_minutes":
                            switch_by_valve.get(vid, {}).get(sid, 0),
                    })
                    switches_by_valve[vid] = \
                        switches_by_valve.get(vid, 0) + 1
        else:
            gap = (g["start_dt"] - last_end[vid]).total_seconds() / 60.0
            gap_minutes = round(gap, 2)
            if prev_sid != sid:
                switches.append({
                    "valve_id": vid, "valve_name": g["valve_name"],
                    "from_source_id": prev_sid,
                    "from_source_name": src_by_id[prev_sid]["name"]
                        if prev_sid in src_by_id else None,
                    "to_source_id": sid,
                    "to_source_name": src_by_id[sid]["name"],
                    "at": _hm(g["start_dt"]), "at_min": g["start_min"],
                    "kind": "switch", "gap_minutes": gap_minutes,
                    "required_minutes": switch_by_valve.get(vid, {}).get(sid, 0),
                })
                switches_by_valve[vid] = switches_by_valve.get(vid, 0) + 1
        last_src[vid] = sid
        last_end[vid] = g["end_dt"]
        switched_from = None
        if prev_sid is not None and prev_sid != sid:
            switched_from = prev_sid
        elif prev_sid is None and sid != bound_by_valve.get(vid):
            switched_from = bound_by_valve.get(vid)
        group_rows.append({
            "valve_id": vid, "valve_name": g["valve_name"],
            "zone_ids": g["zone_ids"], "zone_names": g["zone_names"],
            "start": _hm(g["start_dt"]), "end": _hm(g["end_dt"]),
            "water_l": round(g["total_l"], 2),
            "source_id": sid, "source_name": src_by_id[sid]["name"],
            "klass": gklass,
            "switched_from": switched_from,
            "gap_minutes": gap_minutes,
            "required_minutes": switch_by_valve.get(vid, {}).get(sid, 0),
        })

    # 未覆盖运行组诊断
    uncovered_rows = []
    if not feasible:
        for gi, g in enumerate(groups):
            if assignment.get(gi) is not None:
                continue
            if not g["candidates"]:
                conflicts = []
                no_candidate = True
            else:
                conflicts = diagnose_group(
                    g, gi, groups, sources, assignment, src_by_id,
                    switch_by_valve.get(g["valve_id"], {}), bound_by_valve)
                no_candidate = False
            uncovered_rows.append({
                "valve_id": g["valve_id"], "valve_name": g["valve_name"],
                "zone_ids": g["zone_ids"], "zone_names": g["zone_names"],
                "start": _hm(g["start_dt"]), "end": _hm(g["end_dt"]),
                "water_l": round(g["total_l"], 2),
                "no_candidate": no_candidate,
                "candidate_sources": conflicts,
            })

    # 阀门汇总
    valve_rows = []
    for v in sorted(valves, key=lambda x: x["id"]):
        vid = v["id"]
        grows = [r for r in group_rows if r["valve_id"] == vid]
        if not grows:
            continue
        valve_rows.append({
            "valve_id": vid, "valve_name": v["name"],
            "bound_source_id": bound_by_valve.get(vid),
            "bound_source_name": src_by_id[bound_by_valve[vid]]["name"]
                if bound_by_valve.get(vid) in src_by_id else None,
            "start": grows[0]["start"], "end": grows[-1]["end"],
            "water_l": round(sum(r["water_l"] for r in grows), 2),
            "switches": switches_by_valve.get(vid, 0),
            "uncovered_l": round(
                sum(r["water_l"] for r in grows if r["klass"] == UNCOVERED), 2),
        })

    # 水源汇总
    source_rows = []
    for s in sources:
        used = source_used[s["id"]]
        cap = s.get("daily_cap_l")
        source_rows.append({
            "source_id": s["id"], "source_name": s["name"],
            "used_l": round(used, 2),
            "daily_cap_l": cap,
            "cap_remaining_l": round(cap - used, 2) if cap is not None else None,
            "source_concur": s.get("source_concur", 1),
        })

    # 分区汇总（子段水量按该时刻并发分区数均摊回各分区）
    zone_agg = {}
    for r in piece_rows:
        for zid in r["zone_ids"]:
            zname = zone_by_id.get(zid, {}).get("name", f"#{zid}")
            a = zone_agg.setdefault(zid, {
                "zone_id": zid, "zone_name": zname,
                RISK: 0.0, REVIEW: 0.0, SAFE: 0.0,
                INSUFFICIENT: 0.0, UNCOVERED: 0.0,
                "sources": [],
            })
            a[r["klass"]] += r["water_l"] / r["multiplier"]
            if r["source_id"] is not None and r["source_id"] not in a["sources"]:
                a["sources"].append(r["source_id"])
    zone_rows = []
    for zid in sorted(zone_agg):
        a = zone_agg[zid]
        a["risk_l"] = round(a.pop(RISK), 2)
        a["review_l"] = round(a.pop(REVIEW), 2)
        a["safe_l"] = round(a.pop(SAFE), 2)
        a["insufficient_l"] = round(a.pop(INSUFFICIENT), 2)
        a["uncovered_l"] = round(a.pop(UNCOVERED), 2)
        a["source_names"] = [src_by_id[s]["name"] for s in a.pop("sources")
                             if s in src_by_id]
        a["klass"] = UNCOVERED if a["uncovered_l"] > 0 else \
            RISK if a["risk_l"] > 0 else \
            INSUFFICIENT if a["insufficient_l"] > 0 else \
            REVIEW if a["review_l"] > 0 else SAFE
        zone_rows.append(a)

    return {
        "date": day.isoformat(),
        "slot_minutes": slot_min,
        "class_labels": CLASS_LABEL,
        "feasible": feasible,
        "exhaustive": result["exhaustive"],
        "summary": {
            "total_l": round(sum(g["total_l"] for g in groups), 2),
            "risk_l": round(totals[RISK], 2),
            "review_l": round(totals[REVIEW], 2),
            "safe_l": round(totals[SAFE], 2),
            "insufficient_l": round(totals[INSUFFICIENT], 2),
            "uncovered_l": round(totals[UNCOVERED], 2),
            "objective_l": round(totals[RISK] + totals[INSUFFICIENT], 2),
            "switches": sum(v["switches"] for v in valve_rows),
        },
        "sources": source_rows,
        "valves": valve_rows,
        "zones": zone_rows,
        "switches": switches,
        "groups": group_rows,
        "pieces": piece_rows,
        "uncovered": uncovered_rows,
    }
