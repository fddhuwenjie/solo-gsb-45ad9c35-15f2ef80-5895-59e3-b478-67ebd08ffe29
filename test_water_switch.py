"""水质异常替代水源方案回归测试。

运行（无需启动服务，纯模块级）：

    python -m unittest test_water_switch -v

覆盖边界：
- 状态边界落在灌溉运行中：子段切分核算差水，但整个运行只能用一个水源；
- 相邻运行首尾相接（间隔 0）不能换源，跨窗口且间隔足够才允许切换；
- 日供水上限、同源并发上限两类容量约束；
- 一级目标最小化风险+证据不足，二级目标最小化切换次数（字典序）；
- 已绑定阀门首次沿用绑定水源不计切换、换源计一次；未绑定阀门首次不计；
- 无解时给出未覆盖水量与逐候选水源的冲突原因；
- 同阀门并发分区合并为一个运行组，水量按倍数计算；
- 半开区间：采样时刻起新状态，无历史样本前置段为证据不足。
"""

import unittest
from datetime import date

import water_quality as wq
import water_switch as ws

DAY = date(2026, 10, 1)
R = wq.default_ranges()


def sample(sid, ts, nitrate=10.0, ph=7.0, ec=1.0, turb=2.0):
    return {"id": sid * 10000 + int(ts[11:13]) + int(ts[8:10]) * 100,
            "source_id": sid, "ts": ts,
            "ph": ph, "ec": ec, "turbidity": turb, "nitrate": nitrate}


def sources(cap1=None, cap2=None, concur1=2, concur2=2):
    return [
        {"id": 1, "name": "机井", "daily_cap_l": cap1, "source_concur": concur1},
        {"id": 2, "name": "雨水", "daily_cap_l": cap2, "source_concur": concur2},
    ]


def vs(rain_north=30, rain_mid=60, include_bound=True):
    rows = []
    if include_bound:
        rows += [
            {"valve_id": 1, "source_id": 1, "switch_minutes": 0},
            {"valve_id": 2, "source_id": 1, "switch_minutes": 0},
            {"valve_id": 3, "source_id": 1, "switch_minutes": 0},
        ]
    rows += [
        {"valve_id": 1, "source_id": 2, "switch_minutes": rain_north},
        {"valve_id": 2, "source_id": 2, "switch_minutes": rain_mid},
        {"valve_id": 3, "source_id": 2, "switch_minutes": rain_mid},
    ]
    return rows


# 机井：9-29、9-30 连续两次硝酸盐越界 -> 10-01 全天沿用「超标」
WELL_EXCEEDED_ALL_DAY = {
    1: [sample(1, "2026-09-29T20:00:00", 40),
        sample(1, "2026-09-30T20:00:00", 40)],
}
# 雨水：当日 06:00 首条正常采样 -> 06:00 前证据不足，之后正常
RAIN_FROM_0600 = {
    2: [sample(2, "2026-10-01T06:00:00", 10)],
}


def make_inputs(extra_samples=None, caps=(None, None), concurs=(2, 2)):
    sbs = {}
    sbs.update(WELL_EXCEEDED_ALL_DAY)
    sbs.update(RAIN_FROM_0600)
    if extra_samples:
        sbs.update(extra_samples)
    valves = [
        {"id": 1, "name": "北", "flow_lmin": 25, "concur": 1, "source_id": 1},
        {"id": 2, "name": "中", "flow_lmin": 25, "concur": 1, "source_id": 1},
        {"id": 3, "name": "苗", "flow_lmin": 10, "concur": 1, "source_id": None},
    ]
    zones = [
        {"id": 10, "name": "番茄", "valve_id": 1},
        {"id": 11, "name": "草莓", "valve_id": 2},
        {"id": 12, "name": "黄瓜", "valve_id": 2},
        {"id": 13, "name": "育苗", "valve_id": 3},
    ]
    items = [
        {"zone_id": 10, "start_slot": 32, "length": 8},   # 08:00-10:00 3000L
        {"zone_id": 11, "start_slot": 22, "length": 8},   # 05:30-07:30 3000L
        {"zone_id": 12, "start_slot": 72, "length": 6},   # 18:00-19:30 2250L
        {"zone_id": 13, "start_slot": 30, "length": 3},   # 07:30-08:15 450L
    ]
    return dict(
        zones=zones, items=items, valves=valves,
        sources=sources(caps[0], caps[1], concurs[0], concurs[1]),
        samples_by_source=sbs,
        ranges_by_source={1: R, 2: R}, valve_sources=vs())


class PlanTests(unittest.TestCase):
    def plan(self, **overrides):
        inp = make_inputs(
            caps=overrides.pop("caps", (None, None)),
            concurs=overrides.pop("concurs", (2, 2)))
        inp.update(overrides)
        return ws.plan_day(day=DAY, **inp)

    def test_switch_removes_risk_and_counts_first_switch(self):
        # 全组切雨水：番茄 3000、黄瓜 2250、草莓 06:00 后 2250 正常
        res = self.plan()
        self.assertTrue(res["feasible"])
        self.assertEqual(res["summary"]["risk_l"], 0.0)
        # 草莓 05:30-06:00 证据不足 25*30=750L（组内边界，整组仍雨水）
        self.assertEqual(res["summary"]["insufficient_l"], 750.0)
        # 安全：草莓 06:00-07:30 2250 + 番茄 3000 + 黄瓜 2250 + 育苗 450
        self.assertEqual(res["summary"]["safe_l"], 7950.0)
        # 北、中两个已绑定阀门首次换源各 1 次；苗床未绑定不计
        self.assertEqual(res["summary"]["switches"], 2)
        names = {g["valve_name"]: g["source_name"] for g in res["groups"]}
        self.assertEqual(names["北"], "雨水")

    def test_mid_run_status_boundary_does_not_split_source(self):
        # 草莓运行 05:30-07:30 跨越 06:00 状态边界：必须整组同一水源
        res = self.plan()
        straw = [g for g in res["groups"]
                 if "草莓" in g["zone_names"]][0]
        self.assertEqual(straw["source_id"], 2)
        # 但子段明细仍按 06:00 切开：证据不足 750 + 正常 2250
        pcs = [p for p in res["pieces"] if 11 in p["zone_ids"]]
        self.assertEqual(len(pcs), 2)
        self.assertEqual({p["source_id"] for p in pcs}, {2})
        klasses = sorted(p["klass"] for p in pcs)
        self.assertEqual(klasses, ["insufficient", "safe"])

    def test_daily_capacity_forces_risk_water(self):
        # 雨水只给 6000L：番茄 3000 + 黄瓜 2250 + 育苗 450 = 5700 用雨水，
        # 草莓 3000 被迫机井（全风险）；这是「风险+证据不足」最小的选择
        # （改让草莓用雨水会挤掉同量的纯风险节省，证据不足部分更差）。
        res = self.plan(caps=(None, 6000))
        self.assertTrue(res["feasible"])
        self.assertEqual(res["summary"]["risk_l"], 3000.0)
        used = {s["source_id"]: s["used_l"] for s in res["sources"]}
        self.assertLessEqual(used[2], 6000.0 + 1e-9)

    def test_zero_capacity_all_uncovered_with_reasons(self):
        res = self.plan(caps=(0, 0))
        self.assertFalse(res["feasible"])
        self.assertGreater(res["summary"]["uncovered_l"], 0)
        self.assertTrue(res["uncovered"])
        for u in res["uncovered"]:
            self.assertTrue(u["candidate_sources"])
            for c in u["candidate_sources"]:
                self.assertTrue(any("日供水上限" in r for r in c["reasons"]))

    def test_no_candidate_valve_uncovered(self):
        inp = make_inputs()
        # 删掉阀门 3（苗床）的全部候选（含隐式绑定——它本就未绑定）
        inp["valve_sources"] = [r for r in inp["valve_sources"]
                                if r["valve_id"] != 3]
        res = ws.plan_day(day=DAY, **inp)
        self.assertFalse(res["feasible"])
        u = [x for x in res["uncovered"] if x["valve_id"] == 3][0]
        self.assertTrue(u["no_candidate"])
        self.assertEqual(u["water_l"], 450.0)

    def test_same_source_concurrency_blocks_overlap(self):
        # 番茄 08:00-10:00 与育苗 07:30-08:15 在 08:00-08:15 重叠；
        # 雨水并发 1 -> 两组不能同时雨水。
        res = self.plan(concurs=(2, 1))
        used_rain = {g["valve_name"] for g in res["groups"]
                     if g["source_id"] == 2}
        self.assertTrue("北" in used_rain or "苗" in used_rain)
        self.assertFalse("北" in used_rain and "苗" in used_rain)

    def test_concurrency_touching_intervals_is_allowed(self):
        # 半开区间：[07:30,08:00) 与 [08:00,08:15) 端点相接不算重叠。
        groups = ws.build_groups(
            [{"id": 3, "name": "苗", "flow_lmin": 10, "source_id": None}],
            [{"id": 13, "name": "育苗", "valve_id": 3}],
            [{"zone_id": 13, "start_slot": 30, "length": 2}],
            {2: ws.build_segments_by_source(RAIN_FROM_0600, {2: R}, DAY)[2]},
            {3: [{"valve_id": 3, "source_id": 2, "switch_minutes": 0}]},
            DAY)
        # 07:30-08:00 单组即可（无重叠分区）
        self.assertEqual(len(groups), 1)

    def test_switch_gap_blocks_re_entering_source(self):
        # 同一阀门三次运行：A 08:00-08:30、B 09:00-09:30、C 10:00-10:30。
        # 机井全天超标；雨水全天正常但日上限只够一次运行（1000L）。
        # 最优：A 用雨水（750L 安全），B 被迫机井；C 想再回雨水——
        # 距 B 结束仅 30 分钟 < 雨水切换耗时 60，被挡 -> C 只能机井（风险）。
        valves = [{"id": 2, "name": "中", "flow_lmin": 25,
                   "concur": 1, "source_id": 1}]
        zones = [{"id": 40, "name": "甲", "valve_id": 2},
                 {"id": 41, "name": "乙", "valve_id": 2},
                 {"id": 42, "name": "丙", "valve_id": 2}]
        items = [{"zone_id": 40, "start_slot": 32, "length": 2},
                 {"zone_id": 41, "start_slot": 36, "length": 2},
                 {"zone_id": 42, "start_slot": 40, "length": 2}]
        sbs = {1: WELL_EXCEEDED_ALL_DAY[1],
               2: [sample(2, "2026-09-30T20:00:00", 10)]}
        regs = [{"valve_id": 2, "source_id": 1, "switch_minutes": 0},
                {"valve_id": 2, "source_id": 2, "switch_minutes": 60}]
        res = ws.plan_day(zones, items, valves,
                          sources(None, 750, 2, 2), sbs,
                          {1: R, 2: R}, regs, DAY)
        chosen = {g["zone_names"][0]: g["source_id"] for g in res["groups"]}
        self.assertEqual(chosen["甲"], 2)
        self.assertEqual(chosen["乙"], 1)
        self.assertEqual(chosen["丙"], 1)   # 回切雨水被 30 < 60 挡住
        self.assertEqual(res["summary"]["risk_l"], 1500.0)
        # 把雨水切换耗时放宽到 30（间隔 30 满足）-> C 可回雨水，仍因容量不足而机井
        regs2 = [{"valve_id": 2, "source_id": 1, "switch_minutes": 0},
                 {"valve_id": 2, "source_id": 2, "switch_minutes": 30}]
        res2 = ws.plan_day(zones, items, valves,
                           sources(None, 1500, 2, 2), sbs,
                           {1: R, 2: R}, regs2, DAY)
        chosen2 = {g["zone_names"][0]: g["source_id"] for g in res2["groups"]}
        self.assertEqual(chosen2["丙"], 2)

    def test_switch_minutes_ok_helper(self):
        from datetime import datetime, timedelta
        t0 = datetime(2026, 10, 1, 7, 30)
        self.assertFalse(ws._switch_minutes_ok(1, t0, 700, t0 + timedelta(minutes=630)))
        self.assertTrue(ws._switch_minutes_ok(1, t0, 600, t0 + timedelta(minutes=630)))
        self.assertTrue(ws._switch_minutes_ok(None, None, 999, t0))

    def test_touching_runs_gap_zero_cannot_change_source(self):
        # 中部：分区 A 05:30-06:30、分区 B 06:30-07:30（首尾相接）。
        # 雨水 06:00 起正常：A 用雨水仍带 750L 证据不足；若允许 0 间隔换源，
        # 最优会是「A 雨水 + B 机井（风险）」之类的组间瞬时换源。
        # 因切换只能在边界且间隔须满足耗时，gap=0 时两个水源都不可切换，
        # 故两个首尾相接组必须使用同一水源。
        valves = [{"id": 2, "name": "中", "flow_lmin": 25,
                   "concur": 1, "source_id": 1}]
        zones = [{"id": 20, "name": "甲", "valve_id": 2},
                 {"id": 21, "name": "乙", "valve_id": 2}]
        items = [{"zone_id": 20, "start_slot": 22, "length": 4},
                 {"zone_id": 21, "start_slot": 26, "length": 4}]
        sbs = {1: WELL_EXCEEDED_ALL_DAY[1],
               2: [sample(2, "2026-10-01T06:00:00", 10)]}
        regs = [{"valve_id": 2, "source_id": 1, "switch_minutes": 15},
                {"valve_id": 2, "source_id": 2, "switch_minutes": 15}]
        res = ws.plan_day(zones, items, valves,
                          sources(None, None), sbs, {1: R, 2: R},
                          regs, DAY)
        srcs = {g["zone_names"][0]: g["source_id"] for g in res["groups"]}
        # 0 间隔不能换源：两组必须同水源
        self.assertEqual(srcs["甲"], srcs["乙"])

    def test_zero_switch_minutes_allows_change_after_run(self):
        # 对照：切换耗时为 0 时，首尾相接的两组允许换源（gap 0 ≥ 0）。
        valves = [{"id": 2, "name": "中", "flow_lmin": 25,
                   "concur": 1, "source_id": 1}]
        zones = [{"id": 20, "name": "甲", "valve_id": 2},
                 {"id": 21, "name": "乙", "valve_id": 2}]
        items = [{"zone_id": 20, "start_slot": 22, "length": 4},
                 {"zone_id": 21, "start_slot": 26, "length": 4}]
        sbs = {1: WELL_EXCEEDED_ALL_DAY[1],
               2: [sample(2, "2026-10-01T06:00:00", 10)]}
        regs = [{"valve_id": 2, "source_id": 1, "switch_minutes": 0},
                {"valve_id": 2, "source_id": 2, "switch_minutes": 0}]
        res = ws.plan_day(zones, items, valves,
                          sources(None, None), sbs, {1: R, 2: R},
                          regs, DAY)
        srcs = {g["zone_names"][0]: g["source_id"] for g in res["groups"]}
        self.assertEqual(srcs["甲"], 2)   # 雨水（750 证据不足 < 1500 风险）
        self.assertEqual(srcs["乙"], 2)   # 雨水安全，且 0 间隔切换合法
        self.assertEqual(res["summary"]["risk_l"], 0.0)

    def test_objective_minimize_switches_second(self):
        # 雨水容量无限、并发足够时，全雨水只需 2 次切换；
        # 全机井（绑定水源）0 次切换但全风险 -> 仍选雨水。
        res = self.plan()
        self.assertEqual(res["summary"]["switches"], 2)
        self.assertEqual(res["summary"]["risk_l"], 0.0)

    def test_bound_source_kept_when_no_benefit_zero_switches(self):
        # 两水源都全天正常：沿用绑定机井 -> 0 次切换最优
        sbs = {1: [sample(1, "2026-09-30T20:00:00", 10)],
               2: [sample(2, "2026-09-30T20:00:00", 10)]}
        inp = make_inputs()
        inp["samples_by_source"] = sbs
        res = ws.plan_day(day=DAY, **inp)
        self.assertEqual(res["summary"]["switches"], 0)
        for g in res["groups"]:
            self.assertEqual(g["source_id"], 1)

    def test_unbound_first_source_not_a_switch(self):
        res = self.plan()
        # 苗床未绑定，无论用哪个水源都不出现在 switches 里
        self.assertFalse(any(s["valve_name"] == "苗" for s in res["switches"]))

    def test_overlapping_runs_same_valve_merge_and_multiply(self):
        # 同阀门并发 2：两分区在 08:00-08:30 重叠 -> 合并为一个运行组，
        # 重叠部分水量 ×2。
        valves = [{"id": 1, "name": "北", "flow_lmin": 25,
                   "concur": 2, "source_id": 1}]
        zones = [{"id": 30, "name": "甲", "valve_id": 1},
                 {"id": 31, "name": "乙", "valve_id": 1}]
        items = [{"zone_id": 30, "start_slot": 32, "length": 2},
                 {"zone_id": 31, "start_slot": 32, "length": 4}]
        sbs = {1: WELL_EXCEEDED_ALL_DAY[1],
               2: [sample(2, "2026-09-30T20:00:00", 10)]}
        regs = [{"valve_id": 1, "source_id": 1, "switch_minutes": 0},
                {"valve_id": 1, "source_id": 2, "switch_minutes": 0}]
        res = ws.plan_day(zones, items, valves, sources(None, None, 2, 2),
                          sbs, {1: R, 2: R}, regs, DAY)
        self.assertEqual(len(res["groups"]), 1)
        # 08:00-08:30 重叠 ×2 = 25*30*2=1500；08:30-09:00 单路 750；合计 2250
        self.assertEqual(res["groups"][0]["water_l"], 2250.0)
        mults = sorted(p["multiplier"] for p in res["pieces"])
        self.assertEqual(mults, [1, 2])

    def test_half_open_sample_boundary_at_slot_start(self):
        # 采样时刻恰为槽起点 08:00：[08:00,...) 起采用新状态。
        groups = ws.build_groups(
            [{"id": 1, "name": "北", "flow_lmin": 25, "source_id": 1}],
            [{"id": 10, "name": "番茄", "valve_id": 1}],
            [{"zone_id": 10, "start_slot": 32, "length": 2}],
            {2: [{"start": "2026-10-01T00:00:00", "end": "2026-10-01T08:00:00",
                  "status": "insufficient", "triggers": [],
                  "metric_states": None, "source": ("insufficient", None)},
                 {"start": "2026-10-01T08:00:00", "end": "2026-10-02T00:00:00",
                  "status": "normal", "triggers": [],
                  "metric_states": None, "source": ("sample", None)}]},
            {1: [{"valve_id": 1, "source_id": 2, "switch_minutes": 0}]},
            DAY)
        # 边界在窗口起点：不再切分，整组一个子段且为正常
        self.assertEqual(len(groups[0]["pieces"]), 1)
        self.assertEqual(groups[0]["pieces"][0]["classes"][2], "safe")

    def test_validation_helpers(self):
        self.assertIsNone(ws.validate_capacity(""))
        self.assertIsNone(ws.validate_capacity(None))
        self.assertEqual(ws.validate_capacity("0"), 0.0)
        self.assertEqual(ws.validate_capacity(120), 120.0)
        with self.assertRaises(ValueError):
            ws.validate_capacity(-1)
        with self.assertRaises(ValueError):
            ws.validate_capacity("abc")
        self.assertEqual(ws.validate_concur(3), 3)
        with self.assertRaises(ValueError):
            ws.validate_concur(0)
        with self.assertRaises(ValueError):
            ws.validate_concur(1.5)
        self.assertEqual(ws.validate_switch_minutes(0), 0)
        with self.assertRaises(ValueError):
            ws.validate_switch_minutes(-2)

    def test_parse_day_rejects_datetime(self):
        with self.assertRaises(ValueError):
            ws.parse_day("2026-10-01T08:00")


if __name__ == "__main__":
    unittest.main()
