"""水质异常替代水源优选核心（不改灌溉时刻，只改每个子段的水源）。

前提与切分
==========
当天已有一份 15 分钟轮灌计划（各区灌溉时刻固定，不允许挪动）。水质异常时，
操作员为各阀门登记若干可用替代水源，并为水源配置：

- ``day_cap_l``    日供水上限（L，全天所有分区取自该水源的水量之和不得超过）；
- ``same_concur``  同源并发上限（同一时刻最多多少路灌溉同时使用该水源）。

阀门登记的每个可用水源带「切换耗时」``switch_min``（分钟）：阀门从别的水源
切到该水源至少需要的空闲时间。

系统沿用 ``water_quality`` 的「连续两次」状态机与 ``water_trace`` 的
**左闭右开**状态区间（采样时刻起新状态生效、末条向后延续、首条之前沿用
最近历史采样、无历史样本为证据不足），把每个分区当日灌溉时段在**水质状态
边界**（采样时刻）切分为若干子段（piece）。子段是分配的最小原子单位：
**每个子段只能整段使用一个水源**，不允许在子段内部再切。

切换规则
========
切换只能发生在子段边界，且边界时刻必须满足该水源的切换耗时：

- 同一阀门在子段开始时刻若正被其它水源占用（另一分区尚未结束，含同时开始），
  不能切换（半开区间：恰在该时刻结束的占用视为已释放）；
- 从其它水源切到本水源时，阀门在本时段开始前必须已空闲满 ``switch_min``
  分钟（距阀门最近一次实际用水结束的间隔，含「切回」原水源的情形）；
  与最近用水为同一水源时不算切换、不要求空闲。

由此，一个分区连续灌溉时段内部（相邻子段之间空闲为 0）一旦选定水源便不能
再换——这是「不改动灌溉时刻」的直接推论；真正可能的切换发生在两个不同
灌溉轮次之间、且阀门空闲足够的边界上。

优选目标（字典序）
==================
0. **未覆盖水量最少**：覆盖是可行性前提——即使某水源处于风险状态，
   也必须先保证灌溉有水可用（不能为了少切换而不供水）；
1. 在全覆盖（未覆盖=0）的方案中，风险水量（取自「超标」状态区间）最少；
2. 证据不足水量（无任何状态证据时段的水量）最少；
3. 切换次数最少。

只有当硬约束（容量 / 同源并发 / 切换耗时 / 阀门登记）使任何水源都放不下
某个子段时，该子段才计为未覆盖；找不到全覆盖方案时 ``feasible=false``，
并逐子段列出未覆盖水量与具体冲突原因（容量不足 / 同源并发 /
切换耗时不足 / 阀门未登记水源）。

本模块只做纯计算，不读写数据库；状态区间构建复用 ``water_quality`` 与
``water_trace``，保证与「用水回溯」同一套状态结论。
"""

from datetime import datetime

import water_quality as wq
import water_trace as wt

RISK = wt.RISK
REVIEW = wt.REVIEW
SAFE = wt.SAFE
INSUFFICIENT = wt.INSUFFICIENT

CLASS_LABEL = wt.CLASS_LABEL

# 局部启发式代价：风险最贵、证据不足次之、观察/正常最便宜（二者在正式
# 字典序目标中均不产生风险或证据不足水量）
_CLASS_COST = {RISK: 3, INSUFFICIENT: 2, REVIEW: 1, SAFE: 0}

_EPS = 1e-6


# ---------------------------------------------------------------- 时间工具

def parse_day(raw):
    return wt.parse_day(raw)


def hm(minute):
    """75 -> '01:15'。"""
    minute = int(round(minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _dt(ts):
    return datetime.fromisoformat(ts)


def _r(x):
    """水量 / 分钟展示：750.0 -> '750'，750.5 -> '750.5'。"""
    x = round(float(x), 2)
    return str(int(x)) if x == int(x) else str(x)


# ---------------------------------------------------------------- 状态区间（分钟）

def build_source_segments(samples_by_source, ranges_by_source, day):
    """把各水源当日半开状态区间换算为相对当日 00:00 的分钟区间。

    与 water_trace.trace_day 的预处理完全一致（全量重算 + 历史沿用），
    返回 ``{source_id: [{start_min,end_min,status,klass,triggers,
                        source_kind,sample_id,sample_ts}]}``；
    区间左闭右开、互不重叠、按起点升序，合起来覆盖 [0,1440)。
    未出现在 samples_by_source 的水源由调用方按「证据不足」补全天区间。
    """
    day_start = datetime.combine(day, datetime.min.time())
    result = {}
    for sid, raw in samples_by_source.items():
        ordered = sorted(raw, key=lambda s: (s["ts"], s.get("id", 0)))
        recomputed, _summary = wq.recompute_source(
            [{k: s[k] for k in
              ("id", "source_id", "ts", "ph", "ec", "turbidity", "nitrate")}
             for s in ordered],
            ranges_by_source[sid])
        today_samples = [s for s in recomputed
                         if s["ts"][:10] == day.isoformat()]
        carry = None
        for s in recomputed:
            if _dt(s["ts"]) < day_start:
                carry = s
            else:
                break
        segs = []
        for g in wt.status_segments(today_samples, day, carry):
            lo = max(0.0, (_dt(g["start"]) - day_start).total_seconds() / 60.0)
            hi = min(1440.0, (_dt(g["end"]) - day_start).total_seconds() / 60.0)
            if hi <= lo:
                continue
            kind, src_sample = g["source"]
            segs.append({
                "start_min": lo,
                "end_min": hi,
                "status": g["status"],
                "klass": wt._STATUS_CLASS.get(g["status"], INSUFFICIENT),
                "triggers": list(g["triggers"]),
                "source_kind": kind,
                "sample_id": src_sample["id"] if src_sample else None,
                "sample_ts": src_sample["ts"] if src_sample else None,
            })
        result[sid] = segs
    return result


def insufficient_segments():
    """无任何采样的水源：全天 [0,1440) 证据不足。"""
    return [{
        "start_min": 0.0, "end_min": 1440.0,
        "status": INSUFFICIENT, "klass": INSUFFICIENT, "triggers": [],
        "source_kind": wt.NO_HISTORY, "sample_id": None, "sample_ts": None,
    }]


def klass_at(segments, minute):
    """半开区间 [start,end) 中 minute 时刻的状态段；整点归右侧区间。"""
    for g in segments:
        if g["start_min"] - _EPS <= minute < g["end_min"] - _EPS:
            return g
    return segments[-1] if segments else None


# ---------------------------------------------------------------- 数据规整

def normalize_valve_sources(raw, source_ids):
    """校验阀门可用水源登记，返回 {valve_id: [{source_id, switch_min}]}。

    raw: {valve_id: [{source_id, switch_min}]}；source_ids 为全部水源 ID。
    - 同一阀门同一水源只能登记一次；
    - switch_min 必须为 0~1440 的非负整数分钟。
    """
    clean = {}
    if not isinstance(raw, dict):
        raise ValueError("阀门水源登记必须为对象")
    for vid_key, lst in raw.items():
        try:
            vid = int(vid_key)
        except (TypeError, ValueError):
            raise ValueError(f"阀门 ID 非法：{vid_key}")
        if not isinstance(lst, list):
            raise ValueError(f"阀门 {vid} 的可用水源必须为数组")
        seen = set()
        out = []
        for i, item in enumerate(lst):
            if not isinstance(item, dict):
                raise ValueError(f"阀门 {vid} 第 {i + 1} 条登记必须为对象")
            sid = item.get("source_id")
            if sid in (None, ""):
                raise ValueError(f"阀门 {vid} 第 {i + 1} 条缺少水源 ID")
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                raise ValueError(f"阀门 {vid} 第 {i + 1} 条水源 ID 非法")
            if sid not in source_ids:
                raise ValueError(f"阀门 {vid} 登记了不存在的水源 {sid}")
            if sid in seen:
                raise ValueError(f"阀门 {vid} 重复登记水源 {sid}")
            seen.add(sid)
            sw = item.get("switch_min", 0)
            if isinstance(sw, bool) or not isinstance(sw, int):
                raise ValueError(f"阀门 {vid} 水源 {sid} 的切换耗时必须为整数分钟")
            if not (0 <= sw <= 1440):
                raise ValueError(f"阀门 {vid} 水源 {sid} 的切换耗时须在 0~1440 分钟")
            out.append({"source_id": sid, "switch_min": sw})
        out.sort(key=lambda x: x["source_id"])
        clean[vid] = out
    return clean


def normalize_source_meta(raw):
    """校验水源容量配置：{sid: {id,name,day_cap_l,same_concur}}。

    day_cap_l 为非负数字；same_concur 为正整数。
    """
    meta = {}
    for sid, m in raw.items():
        sid = int(sid)
        cap = m.get("day_cap_l")
        if cap is None:
            raise ValueError(f"水源 {m.get('name', sid)} 缺少日供水上限")
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            raise ValueError(f"水源 {m.get('name', sid)} 日供水上限必须为数字")
        if cap != cap or cap < 0:
            raise ValueError(f"水源 {m.get('name', sid)} 日供水上限不能为负 / NaN")
        concur = m.get("same_concur")
        if isinstance(concur, bool) or not isinstance(concur, int) or concur < 1:
            raise ValueError(f"水源 {m.get('name', sid)} 同源并发上限必须为正整数")
        meta[sid] = {"id": sid, "name": m.get("name", str(sid)),
                     "day_cap_l": cap, "same_concur": concur}
    return meta


# ---------------------------------------------------------------- 占用板

def max_overlap(intervals):
    """扫描线：[(lo,hi)] 区间中同一时刻的最大重叠数（半开区间，相接不重叠）。"""
    events = []
    for lo, hi in intervals:
        if hi - lo > _EPS:
            events.append((lo, 1))
            events.append((hi, -1))
    # 同点先结束（delta=-1）后开始（delta=+1）：[.,b) 与 [b,.) 不算重叠
    events.sort(key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _t, delta in events:
        cur += delta
        best = max(best, cur)
    return best


class _Board:
    """搜索中的全局占用快照（每深入一层复制一份）。"""

    def __init__(self, source_meta, bound_source_by_valve):
        self._meta = source_meta
        self.caps = {sid: float(m["day_cap_l"])
                     for sid, m in source_meta.items()}
        self.usages = {sid: [] for sid in source_meta}  # sid -> [(lo,hi)]
        self.valve_usage = {}                            # vid -> [(lo,hi,sid)]
        self.prev_source = dict(bound_source_by_valve)  # 阀门当前接通水源
        self.switches = []                               # [{at_min,valve_id,...}]

    def copy(self):
        b = _Board.__new__(_Board)
        b._meta = self._meta
        b.caps = dict(self.caps)
        b.usages = {k: list(v) for k, v in self.usages.items()}
        b.valve_usage = {k: list(v) for k, v in self.valve_usage.items()}
        b.prev_source = dict(self.prev_source)
        b.switches = list(self.switches)
        return b

    def check(self, vid, sid, lo, hi, water_l, switch_min):
        """返回 (ok, reasons)。reasons 为不可行时的中文原因列表。"""
        reasons = []

        # 1) 日供水上限
        remain = self.caps.get(sid, 0.0)
        if water_l > remain + _EPS:
            reasons.append(
                f"日供水上限不足：该段需 {_r(water_l)} L，"
                f"该水源仅剩 {_r(remain)} L")

        # 2) 同源并发（半开：恰在 lo 结束的不算并发）
        concur_cap = self._meta[sid]["same_concur"]
        if max_overlap(self.usages.get(sid, []) + [(lo, hi)]) > concur_cap:
            reasons.append(
                f"同源并发冲突：{hm(lo)}–{hm(hi)} 该水源已达 "
                f"{concur_cap} 路并发上限")

        is_switch = self.prev_source.get(vid) is not None and \
            self.prev_source.get(vid) != sid
        vusages = self.valve_usage.get(vid, [])

        # 3) lo 时刻阀门是否正被占用（同一阀门物理上不能同时供两个分区，
        #    即使水源相同也不行；半开区间，恰在 lo 结束的占用已释放）
        busy_other = any(
            u_lo < lo + _EPS and u_hi > lo + _EPS
            for (u_lo, u_hi, s) in vusages)
        if busy_other:
            reasons.append(
                f"{hm(lo)} 阀门正为其他分区供水且未结束，"
                f"无法在该边界使用 / 切换")

        # 4) 切换耗时：从其它水源切到本水源时，阀门在本时段开始前必须已
        #    空闲满 switch_min 分钟，即距阀门最近一次（任意水源）实际用水
        #    结束的间隔——包括刚用完另一水源又「切回」本水源的情形。
        prior_ends = [u_hi for (u_lo, u_hi, s) in vusages
                      if u_hi <= lo + _EPS]
        if prior_ends and is_switch:
            gap = lo - max(prior_ends)
            if gap + _EPS < switch_min:
                if gap <= _EPS:
                    reasons.append(
                        f"切换耗时不足：{hm(lo)} 与上一轮用水相接，"
                        f"切至该水源至少需要 {switch_min} 分钟空闲")
                else:
                    reasons.append(
                        f"切换耗时不足：距阀门上次用水仅 {_r(gap)} 分钟，"
                        f"切至该水源至少需要 {switch_min} 分钟")

        return (not reasons), reasons

    def place(self, vid, sid, lo, hi, switch_min):
        """假定 check 通过后落子。返回是否发生了一次切换。"""
        self.usages.setdefault(sid, []).append((lo, hi))
        self.valve_usage.setdefault(vid, []).append((lo, hi, sid))
        switched = False
        # 阀门当前接通的水源未知（未绑定）时，第一次接通是「初始接线」，
        # 不是切换；只有从已知的别的水源改过来才计一次切换。
        if self.prev_source.get(vid) is not None and \
                self.prev_source.get(vid) != sid:
            self.switches.append({
                "at_min": lo, "valve_id": vid,
                "from_source_id": self.prev_source.get(vid),
                "to_source_id": sid,
            })
            switched = True
        self.prev_source[vid] = sid
        return switched

    def charge(self, sid, water_l):
        self.caps[sid] -= water_l


# ---------------------------------------------------------------- 切分子段

def _zone_window(item, slot_min):
    lo = max(0.0, float(item["start_slot"]) * slot_min)
    hi = min(1440.0, float(item["start_slot"] + item["length"]) * slot_min)
    return lo, hi


def split_zone(lo, hi, registered, segments_by_source):
    """在所有已登记水源的状态边界上切分窗口 [lo,hi)。

    registered: [{source_id, switch_min}]
    返回 [{start_min,end_min,minutes, sources:{sid: segment}}]，
    相邻子段若各水源归类与触发指标完全一致会被合并（切点无实际状态变化）。
    """
    cuts = {lo, hi}
    for reg in registered:
        for g in segments_by_source.get(reg["source_id"], []):
            if lo + _EPS < g["start_min"] < hi - _EPS:
                cuts.add(g["start_min"])
    bounds = sorted(cuts)
    pieces = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= _EPS:
            continue
        source_states = {}
        for reg in registered:
            source_states[reg["source_id"]] = klass_at(
                segments_by_source.get(reg["source_id"], []), a)
        pieces.append({"start_min": a, "end_min": b, "minutes": b - a,
                       "sources": source_states})
    merged = []
    for p in pieces:
        sig = tuple((sid, g["klass"] if g else None,
                     tuple(g["triggers"]) if g else ())
                    for sid, g in sorted(p["sources"].items()))
        if merged and merged[-1][1] == sig and \
                abs(merged[-1][0]["end_min"] - p["start_min"]) < _EPS:
            merged[-1][0]["end_min"] = p["end_min"]
            merged[-1][0]["minutes"] = (merged[-1][0]["end_min"]
                                        - merged[-1][0]["start_min"])
        else:
            merged.append([dict(p), sig])
    return [m[0] for m in merged]


# ---------------------------------------------------------------- 优选

def solve(zones, items, valves, segments_by_source, source_meta,
          valve_sources, slot_min=15, node_limit=200_000):
    """求解替代水源方案（纯计算）。

    zones:   [{id,name,valve_id}]
    items:   [{zone_id,start_slot,length}]
    valves:  [{id,name,flow_lmin,source_id}]（source_id 为当前绑定，
             方案首段沿用绑定水源时不计切换；改作他用计 1 次切换）
    segments_by_source: build_source_segments 输出
    source_meta:        normalize_source_meta 输出
    valve_sources:      normalize_valve_sources 输出
    """
    valve_by_id = {v["id"]: v for v in valves}
    zone_by_id = {z["id"]: z for z in zones}
    reg_by_valve = valve_sources
    bound = {v["id"]: v.get("source_id") for v in valves}

    # ---- 构造每个分区的子段 ----
    zone_blocks = []
    for it in sorted(items, key=lambda i: (i["start_slot"], i["zone_id"])):
        zone = zone_by_id.get(it["zone_id"])
        if zone is None:
            continue
        vid = zone.get("valve_id")
        valve = valve_by_id.get(vid) if vid is not None else None
        lo, hi = _zone_window(it, slot_min)
        if hi - lo <= _EPS or valve is None:
            continue
        flow = float(valve["flow_lmin"])
        registered = reg_by_valve.get(vid, [])
        pieces = split_zone(lo, hi, registered, segments_by_source)
        for p in pieces:
            p["flow_lmin"] = flow
            p["water_l"] = round(flow * p["minutes"], 2)
            p["valve_id"] = vid
        zone_blocks.append({
            "zone": zone, "valve": valve, "lo": lo, "hi": hi,
            "pieces": pieces, "registered": registered,
        })

    # 受约束最强（登记水源最少、开始最早）的分区先搜，尽快拿到紧的 incumbent
    zone_blocks.sort(key=lambda b: (len(b["registered"]), b["lo"],
                                    b["zone"]["id"]))

    def switch_min_of(vid, sid):
        for reg in reg_by_valve.get(vid, []):
            if reg["source_id"] == sid:
                return reg["switch_min"]
        return 0

    def feasible_candidates(block, piece, board):
        out = []
        lo, hi = piece["start_min"], piece["end_min"]
        vid = block["valve"]["id"]
        for reg in block["registered"]:
            sid = reg["source_id"]
            seg = piece["sources"].get(sid)
            ok, _reasons = board.check(vid, sid, lo, hi, piece["water_l"],
                                       reg["switch_min"])
            if ok:
                out.append({
                    "source_id": sid, "switch_min": reg["switch_min"],
                    "klass": seg["klass"] if seg else INSUFFICIENT,
                    "triggers": list(seg["triggers"]) if seg else [],
                })
        return out

    # ---- 贪心初解（给 DFS 一个紧上界）----
    greedy = _Board(source_meta, bound)
    greedy_assign = {}
    for bi, block in enumerate(zone_blocks):
        vid = block["valve"]["id"]
        picks = []
        for p in block["pieces"]:
            cands = feasible_candidates(block, p, greedy)
            if not cands:
                picks.append(None)
                continue
            # 贪心只追求全覆盖：固定取 ID 最小的可行水源，不按水质偏好，
            # 避免局部偏好提前吃光替代水源容量而漏掉本可覆盖的子段。
            # 方案质量由 DFS 按字典序目标全局优化。
            cands.sort(key=lambda c: c["source_id"])
            c = cands[0]
            greedy.place(vid, c["source_id"], p["start_min"],
                         p["end_min"], c["switch_min"])
            greedy.charge(c["source_id"], p["water_l"])
            picks.append(c["source_id"])
        greedy_assign[bi] = picks

    def score(assign, board):
        """字典序目标：未覆盖水量 > 风险 > 证据不足 > 切换次数。

        覆盖是可行性前提（宁可多切换也要先保证供水），因此未覆盖列第一级；
        在同样全覆盖（未覆盖=0）的方案里再依次最小化风险 / 证据不足 / 切换。
        """
        risk = insuff = uncovered = 0.0
        for bi, block in enumerate(zone_blocks):
            for p, sid in zip(block["pieces"], assign.get(bi, [])):
                if sid is None:
                    uncovered += p["water_l"]
                else:
                    k = (p["sources"][sid]["klass"]
                         if p["sources"].get(sid) else INSUFFICIENT)
                    if k == RISK:
                        risk += p["water_l"]
                    elif k == INSUFFICIENT:
                        insuff += p["water_l"]
        return (round(uncovered, 2), round(risk, 2), round(insuff, 2),
                len(board.switches))

    best_assign = {bi: list(picks) for bi, picks in greedy_assign.items()}
    best_score = score(best_assign, greedy)
    best_board = greedy
    best_tie = _tie_key(best_assign)

    # 扁平化子段序列；无登记水源的分区所有子段只能未覆盖
    seq = [(bi, pi) for bi, b in enumerate(zone_blocks)
           for pi in range(len(b["pieces"]))]
    current = {bi: [None] * len(b["pieces"]) for bi, b in enumerate(zone_blocks)}
    cur_risk = cur_insuff = 0.0
    nodes = 0

    # 每个剩余子段「至少会带来多少风险 / 证据不足水量」的乐观下界：
    # - risk 下界：仅当所有已登记水源在该段都「超标」时才等于整段水量，
    #   否则存在 0 风险选择（下界 0）；
    # - insuff 下界：仅当所有已登记水源都「超标 / 证据不足」时为整段水量
    #   （没有正常或观察可选），否则存在既非风险也非证据不足的选择（下界 0）。
    piece_floor = {}
    for bi, b in enumerate(zone_blocks):
        for pi, p in enumerate(b["pieces"]):
            has_non_risk = any(
                (p["sources"].get(reg["source_id"]) or {}).get("klass") != RISK
                for reg in b["registered"])
            has_clean = any(
                (p["sources"].get(reg["source_id"]) or {}).get("klass")
                in (SAFE, REVIEW) for reg in b["registered"])
            piece_floor[(bi, pi)] = (
                0.0 if has_non_risk else p["water_l"],
                0.0 if has_clean else p["water_l"])

    def dfs(idx, board, uncovered):
        nonlocal best_assign, best_score, best_board, best_tie, nodes
        nonlocal cur_risk, cur_insuff
        nodes += 1
        if nodes > node_limit:
            return
        cur_uncovered = round(uncovered, 2)
        switches = len(board.switches)
        if idx == len(seq):
            sc = (cur_uncovered, round(cur_risk, 2),
                  round(cur_insuff, 2), switches)
            tk = _tie_key(current)
            if sc < best_score or (sc == best_score and tk < best_tie):
                best_score, best_tie = sc, tk
                best_assign = {bi: list(picks)
                               for bi, picks in current.items()}
                best_board = board.copy()
            return

        # 前缀 + 剩余下界的字典序剪枝（score = 未覆盖, 风险, 证据不足, 切换）：
        # - 未覆盖只增不减：前缀已超过最优 -> 放弃；
        # - 未覆盖能追平时，风险（前缀 + 剩余乐观下界）已更高 -> 放弃；
        # - 风险能追平时，证据不足下界更高 -> 放弃；
        # - 前三者都能追平时，切换次数（只增不减）已更多 -> 放弃。
        rest_risk = rest_insuff = 0.0
        for j in range(idx, len(seq)):
            fr, fi = piece_floor[seq[j]]
            rest_risk += fr
            rest_insuff += fi
        lb_risk = round(cur_risk + rest_risk, 2)
        lb_insuff = round(cur_insuff + rest_insuff, 2)
        if cur_uncovered > best_score[0]:
            return
        if cur_uncovered == best_score[0] and lb_risk > best_score[1]:
            return
        if cur_uncovered == best_score[0] and lb_risk == best_score[1] \
                and lb_insuff > best_score[2]:
            return
        if cur_uncovered == best_score[0] and lb_risk == best_score[1] \
                and lb_insuff == best_score[2] and switches > best_score[3]:
            return

        bi, pi = seq[idx]
        block = zone_blocks[bi]
        piece = block["pieces"][pi]
        vid = block["valve"]["id"]
        cands = feasible_candidates(block, piece, board)
        # 覆盖分支优先于留空；覆盖分支里先尝试风险 / 证据不足更低的水源，
        # 以便尽早拿到字典序更小的 incumbent
        cands.sort(key=lambda c: (_CLASS_COST.get(c["klass"], 2),
                                  c["source_id"]))
        branches = [c["source_id"] for c in cands] + [None]  # 未覆盖兜底

        for sid in branches:
            if sid is None:
                current[bi][pi] = None
                dfs(idx + 1, board, uncovered + piece["water_l"])
                current[bi][pi] = None
                continue
            seg = piece["sources"][sid]
            klass = seg["klass"] if seg else INSUFFICIENT
            sw = switch_min_of(vid, sid)
            nb = board.copy()
            nb.place(vid, sid, piece["start_min"], piece["end_min"], sw)
            nb.charge(sid, piece["water_l"])
            current[bi][pi] = sid
            if klass == RISK:
                cur_risk += piece["water_l"]
            elif klass == INSUFFICIENT:
                cur_insuff += piece["water_l"]
            dfs(idx + 1, nb, uncovered)
            if klass == RISK:
                cur_risk -= piece["water_l"]
            elif klass == INSUFFICIENT:
                cur_insuff -= piece["water_l"]
            current[bi][pi] = None

    dfs(0, _Board(source_meta, bound), 0.0)
    exact = nodes <= node_limit

    return _assemble(zone_blocks, best_assign, best_board, best_score,
                     source_meta, bound, reg_by_valve, slot_min, exact,
                     nodes)


def _tie_key(assign):
    return tuple((bi, tuple(s if s is not None else -1 for s in picks))
                 for bi, picks in sorted(assign.items()))


# ---------------------------------------------------------------- 结果组装

def _segment_evidence(seg):
    if not seg:
        return {"source_kind": wt.NO_HISTORY, "sample_id": None,
                "sample_ts": None}
    return {"source_kind": seg["source_kind"], "sample_id": seg["sample_id"],
            "sample_ts": seg["sample_ts"]}


def _assemble(zone_blocks, assign, board, score, source_meta, bound,
              reg_by_valve, slot_min, exact, nodes):
    index_by_block = {id(b): bi for bi, b in enumerate(zone_blocks)}
    zones_out = []
    conflicts = []
    totals = {"risk_l": 0.0, "review_l": 0.0, "safe_l": 0.0,
              "insufficient_l": 0.0, "covered_l": 0.0, "uncovered_l": 0.0,
              "planned_l": 0.0}

    def source_name(sid):
        return source_meta[sid]["name"] if sid in source_meta else None

    for block in sorted(zone_blocks, key=lambda b: (b["lo"], b["zone"]["id"])):
        bi = index_by_block[id(block)]
        zone, valve = block["zone"], block["valve"]
        vid = valve["id"]
        flow = float(valve["flow_lmin"])
        picks = assign[bi]

        pieces_out = []
        for p, sid in zip(block["pieces"], picks):
            water = p["water_l"]
            totals["planned_l"] += water
            if sid is None:
                totals["uncovered_l"] += water
                pieces_out.append({
                    "start_min": round(p["start_min"], 2),
                    "end_min": round(p["end_min"], 2),
                    "start": hm(p["start_min"]), "end": hm(p["end_min"]),
                    "minutes": round(p["minutes"], 2),
                    "water_l": water,
                    "source_id": None, "source_name": None,
                    "klass": INSUFFICIENT, "status": INSUFFICIENT,
                    "triggers": [], "covered": False,
                    "evidence": {"source_kind": "unassigned", "sample_id": None,
                                 "sample_ts": None},
                })
                continue
            seg = p["sources"][sid]
            klass = seg["klass"] if seg else INSUFFICIENT
            totals["covered_l"] += water
            totals[f"{klass}_l"] += water
            pieces_out.append({
                "start_min": round(p["start_min"], 2),
                "end_min": round(p["end_min"], 2),
                "start": hm(p["start_min"]), "end": hm(p["end_min"]),
                "minutes": round(p["minutes"], 2),
                "water_l": water,
                "source_id": sid, "source_name": source_name(sid),
                "klass": klass, "status": seg["status"] if seg else INSUFFICIENT,
                "triggers": list(seg["triggers"]) if seg else [],
                "covered": True,
                "evidence": _segment_evidence(seg),
            })

        # 合并相邻同源子段为对外的方案段
        seg_out = []
        for p in pieces_out:
            if seg_out and p["source_id"] is not None and \
                    seg_out[-1]["source_id"] == p["source_id"] and \
                    abs(seg_out[-1]["end_min"] - p["start_min"]) < _EPS:
                cur = seg_out[-1]
                cur["end_min"] = p["end_min"]
                cur["end"] = p["end"]
                cur["minutes"] = round(cur["minutes"] + p["minutes"], 2)
                cur["water_l"] = round(cur["water_l"] + p["water_l"], 2)
                cur[f"{p['klass']}_l"] = round(
                    cur.get(f"{p['klass']}_l", 0.0) + p["water_l"], 2)
                for t in p["triggers"]:
                    if t not in cur["triggers"]:
                        cur["triggers"].append(t)
            else:
                seg_out.append({
                    "source_id": p["source_id"],
                    "source_name": p["source_name"],
                    "start_min": p["start_min"], "end_min": p["end_min"],
                    "start": p["start"], "end": p["end"],
                    "minutes": p["minutes"], "water_l": p["water_l"],
                    "risk_l": round(p["water_l"] if p["klass"] == RISK else 0.0, 2),
                    "review_l": round(p["water_l"] if p["klass"] == REVIEW else 0.0, 2),
                    "safe_l": round(p["water_l"] if p["klass"] == SAFE else 0.0, 2),
                    "insufficient_l": round(
                        p["water_l"] if p["covered"] and p["klass"] == INSUFFICIENT
                        else 0.0, 2),
                    "triggers": list(p["triggers"]),
                })

        zswitches = [dict(s) for s in board.switches
                     if s["valve_id"] == vid
                     and block["lo"] - _EPS <= s["at_min"] < block["hi"] - _EPS]
        for s in zswitches:
            s["at"] = hm(s["at_min"])
            s["from_source_name"] = source_name(s["from_source_id"])
            s["to_source_name"] = source_name(s["to_source_id"])

        for raw_piece, p in zip(block["pieces"], pieces_out):
            if not p["covered"]:
                conflicts.append(_diagnose_piece(
                    block, raw_piece, assign, zone_blocks, index_by_block,
                    source_meta, bound, reg_by_valve))

        def klass_sum(name):
            return round(sum(p["water_l"] for p in pieces_out
                             if p["covered"] and p["klass"] == name), 2)

        zones_out.append({
            "zone_id": zone["id"],
            "zone_name": zone["name"],
            "valve_id": vid,
            "valve_name": valve["name"],
            "bound_source_id": bound.get(vid),
            "bound_source_name": source_name(bound.get(vid)),
            "flow_lmin": flow,
            "start_slot": int(round(block["lo"] / slot_min)),
            "length": int(round((block["hi"] - block["lo"]) / slot_min)),
            "start_min": round(block["lo"], 2),
            "end_min": round(block["hi"], 2),
            "start": hm(block["lo"]), "end": hm(block["hi"]),
            "minutes": round(block["hi"] - block["lo"], 2),
            "planned_l": round(flow * (block["hi"] - block["lo"]), 2),
            "covered_l": round(sum(p["water_l"] for p in pieces_out
                                   if p["covered"]), 2),
            "uncovered_l": round(sum(p["water_l"] for p in pieces_out
                                     if not p["covered"]), 2),
            "risk_l": klass_sum(RISK),
            "review_l": klass_sum(REVIEW),
            "safe_l": klass_sum(SAFE),
            "insufficient_l": klass_sum(INSUFFICIENT),
            "pieces": pieces_out,
            "segments": seg_out,
            "switches": zswitches,
        })

    sources_out = []
    for sid, meta in sorted(source_meta.items()):
        used = max(0.0, round(meta["day_cap_l"] - board.caps[sid], 2))
        sources_out.append({
            "source_id": sid, "source_name": meta["name"],
            "day_cap_l": round(meta["day_cap_l"], 2),
            "used_l": used,
            "remain_l": round(max(0.0, board.caps[sid]), 2),
            "same_concur": meta["same_concur"],
            "max_concurrent": max_overlap(board.usages.get(sid, [])),
        })

    feasible = totals["uncovered_l"] <= _EPS
    all_switches = []
    for s in board.switches:
        d = dict(s)
        d["at"] = hm(s["at_min"])
        d["from_source_name"] = source_name(s["from_source_id"])
        d["to_source_name"] = source_name(s["to_source_id"])
        all_switches.append(d)
    summary = {
        "planned_l": round(totals["planned_l"], 2),
        "covered_l": round(totals["covered_l"], 2),
        "uncovered_l": round(totals["uncovered_l"], 2),
        "risk_l": round(totals["risk_l"], 2),
        "review_l": round(totals["review_l"], 2),
        "safe_l": round(totals["safe_l"], 2),
        "insufficient_l": round(totals["insufficient_l"], 2),
        "switch_count": len(board.switches),
        "switches": all_switches,
        "feasible": feasible,
        "objective": {
            "uncovered_l": score[0], "risk_l": score[1],
            "insufficient_l": score[2], "switch_count": score[3],
        },
        "exact": exact,
        "search_nodes": nodes,
    }
    return {
        "slot_minutes": slot_min,
        "class_labels": CLASS_LABEL,
        "summary": summary,
        "zones": zones_out,
        "sources": sources_out,
        "conflicts": conflicts,
    }


def _diagnose_piece(block, piece, assign, zone_blocks, index_by_block,
                    source_meta, bound, reg_by_valve):
    """对未覆盖子段，按最优方案里其它（含本分区其它子段）实际占用说明原因。"""
    vid = block["valve"]["id"]
    lo, hi = piece["start_min"], piece["end_min"]
    water = piece["water_l"]
    registered = block["registered"]
    reasons = []
    candidate_status = []

    if not registered:
        reasons.append("阀门未登记任何可用水源")

    # 用最优方案的实际分配重建只读占用板（含本分区其它已覆盖子段）
    db = _Board(source_meta, bound)
    bi_self = index_by_block[id(block)]
    for other in zone_blocks:
        obi = index_by_block[id(other)]
        ovid = other["valve"]["id"]
        for op, osid in zip(other["pieces"], assign[obi]):
            if osid is None:
                continue
            if other is block and abs(op["start_min"] - lo) < _EPS and \
                    abs(op["end_min"] - hi) < _EPS:
                continue  # 跳过待诊断子段自身
            sw = next((r["switch_min"] for r in reg_by_valve.get(ovid, [])
                       if r["source_id"] == osid), 0)
            db.place(ovid, osid, op["start_min"], op["end_min"], sw)
            db.charge(osid, op["water_l"])

    for reg in registered:
        sid = reg["source_id"]
        seg = piece["sources"].get(sid)
        klass = seg["klass"] if seg else INSUFFICIENT
        if klass != SAFE:
            candidate_status.append(
                f"{source_meta[sid]['name']}：{CLASS_LABEL.get(klass, klass)}"
                + ("（" + "、".join(seg["triggers"]) + "）"
                   if seg and seg["triggers"] else ""))
        ok, why = db.check(vid, sid, lo, hi, water, reg["switch_min"])
        if not ok:
            reasons.extend(f"{source_meta[sid]['name']}：{w}" for w in why)

    if registered and not reasons:
        reasons.append("该时段所有已登记水源均与其它分区占用冲突")

    return {
        "zone_id": block["zone"]["id"],
        "zone_name": block["zone"]["name"],
        "valve_id": vid,
        "valve_name": block["valve"]["name"],
        "start_min": round(lo, 2), "end_min": round(hi, 2),
        "start": hm(lo), "end": hm(hi),
        "minutes": round(hi - lo, 2),
        "water_l": water,
        "reasons": sorted(set(reasons)),
        "candidate_status": candidate_status,
    }
