"""替代水源优选回归测试（纯模块级，无需启动服务 / Flask）。

    python -m unittest test_source_switch -v

覆盖：
- 在水质状态边界（采样时刻，含槽中时刻）切分子段，每段单一水源；
- 字典序目标：先全覆盖，再最小化风险 / 证据不足，最后最小化切换次数；
- 日供水上限 / 同源并发 / 阀门占用 / 切换耗时四类硬约束；
- 无登记水源 / 容量不足 / 并发冲突的未覆盖诊断；
- 半开区间相接不并发、同阀门相邻运行沿用同水源不算切换；
- 采样 / 容量 / 登记变化后重算结果不同。
"""

import unittest
from datetime import date

import water_quality as wq
import source_switch as ss

DAY = date(2026, 10, 2)
RANGES = {1: wq.default_ranges(), 2: wq.default_ranges(), 3: wq.default_ranges()}


def sample(sid, source_id, ts, **kw):
    row = {"id": sid, "source_id": source_id, "ts": ts,
           "ph": 7.0, "ec": 1.0, "turbidity": 2.0, "nitrate": 10.0}
    row.update(kw)
    return row


# 1# 机井：硝酸盐连续两次超标，当日 08/09 点仍超标
WELL_RISK = [
    sample(1, 1, "2026-10-01T08:00:00", nitrate=18),
    sample(2, 1, "2026-10-01T14:00:00", nitrate=22),
    sample(3, 1, "2026-10-01T18:00:00", nitrate=34),
    sample(4, 1, "2026-10-01T20:00:00", nitrate=36.5),
    sample(5, 1, "2026-10-02T08:00:00", nitrate=35),
    sample(6, 1, "2026-10-02T09:00:00", nitrate=33),
]
# 雨水池：浊度观察，13:00 连续两次回区间 -> 下午正常
POOL = [
    sample(101, 2, "2026-10-01T07:30:00", turb=5.0, nitrate=6),
    sample(102, 2, "2026-10-01T13:30:00", turb=12.0, nitrate=7.5),
    sample(103, 2, "2026-10-02T07:30:00", turb=8.0, nitrate=6.5),
    sample(104, 2, "2026-10-02T13:00:00", turb=7.0, nitrate=6.5),
]
SAFE_WELL = [sample(201, 3, "2026-10-01T08:00:00", nitrate=10)]


def segs(sources):
    return ss.build_source_segments(sources, {sid: RANGES[sid] for sid in sources}, DAY)


def meta(caps):
    return ss.normalize_source_meta(caps)


DEFAULT_META = meta({
    1: {"name": "机井", "day_cap_l": 8000, "same_concur": 2},
    2: {"name": "雨水池", "day_cap_l": 5000, "same_concur": 2},
    3: {"name": "清水塔", "day_cap_l": 9000, "same_concur": 2},
})


def reg(mapping):
    return ss.normalize_valve_sources(mapping, {1, 2, 3})


def solve(zones, items, valves, segments, source_meta=None, valve_sources=None):
    return ss.solve(
        zones, items, valves, segments,
        source_meta or DEFAULT_META,
        valve_sources or reg({
            1: [{"source_id": 1, "switch_min": 0},
                {"source_id": 2, "switch_min": 30}],
            2: [{"source_id": 1, "switch_min": 0},
                {"source_id": 2, "switch_min": 20}],
            3: [{"source_id": 2, "switch_min": 15}],
        }))


class SegmentSplitTests(unittest.TestCase):

    def test_split_at_sample_boundary_inside_slot(self):
        # 雨水池 08:07 恢复正常：两源（机井持续风险、雨水池观察->正常）的
        # 归类向量在 08:07 改变，08:00-09:00 窗口应被切成两段
        samples = WELL_RISK + [
            sample(105, 2, "2026-10-02T08:07:00", turb=7.0, nitrate=6.5)]
        sg = segs({1: WELL_RISK, 2: samples})
        pieces = ss.split_zone(
            480, 540,
            [{"source_id": 1, "switch_min": 0},
             {"source_id": 2, "switch_min": 0}], sg)
        bounds = [(p["start_min"], p["end_min"]) for p in pieces]
        self.assertIn((480.0, 487.0), bounds)
        self.assertIn((487.0, 540.0), bounds)
        # 水量按分钟比例：两段合计仍是完整 60 分钟
        self.assertEqual(sum(p["minutes"] for p in pieces), 60.0)
        flow = 25.0
        self.assertAlmostEqual(sum(p["minutes"] * flow for p in pieces),
                               1500.0)

    def test_no_sample_source_is_all_day_insufficient(self):
        pieces = ss.split_zone(
            480, 510, [{"source_id": 9, "switch_min": 0}],
            {9: ss.insufficient_segments()})
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]["sources"][9]["klass"], ss.INSUFFICIENT)

    def test_merge_adjacent_pieces_with_identical_states(self):
        # 两条采样若各水源归类不变（仍风险），切点不应产生多余子段
        samples = WELL_RISK
        sg = segs({1: samples})
        pieces = ss.split_zone(
            540, 600, [{"source_id": 1, "switch_min": 0}], sg)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]["sources"][1]["klass"], ss.RISK)


class ObjectiveTests(unittest.TestCase):
    """字典序目标：全覆盖 > 风险 > 证据不足 > 切换次数。"""

    def setUp(self):
        self.sg = segs({1: WELL_RISK, 2: POOL})
        self.valves = [
            {"id": 1, "name": "北", "flow_lmin": 25, "source_id": 1},
            {"id": 2, "name": "中", "flow_lmin": 25, "source_id": 1},
            {"id": 3, "name": "苗", "flow_lmin": 10, "source_id": None},
        ]
        self.zones = [
            {"id": 1, "name": "番茄", "valve_id": 1},
            {"id": 2, "name": "黄瓜", "valve_id": 2},
            {"id": 3, "name": "草莓", "valve_id": 2},
            {"id": 4, "name": "叶菜", "valve_id": 2},
            {"id": 5, "name": "育苗", "valve_id": 3},
        ]
        self.items = [
            {"zone_id": 3, "start_slot": 22, "length": 4},  # 05:30-06:30
            {"zone_id": 1, "start_slot": 32, "length": 8},  # 08:00-10:00
            {"zone_id": 4, "start_slot": 32, "length": 2},  # 08:00-08:30
            {"zone_id": 5, "start_slot": 32, "length": 3},  # 08:00-08:45
            {"zone_id": 2, "start_slot": 72, "length": 6},  # 18:00-19:30
        ]

    def pick(self, res, zid):
        z = next(z for z in res["zones"] if z["zone_id"] == zid)
        return [(p["source_id"], p["klass"]) for p in z["pieces"]]

    def test_full_coverage_and_capacity_drives_tomato_risk(self):
        res = solve(self.zones, self.items, self.valves, self.sg)
        self.assertTrue(res["summary"]["feasible"])
        self.assertEqual(res["summary"]["uncovered_l"], 0)
        # 雨水池 5000 L 容量下非风险水量最多 4950 L；番茄 3000 L 只能用风险机井
        self.assertEqual(res["summary"]["risk_l"], 3000.0)
        self.assertEqual(self.pick(res, 1), [(1, ss.RISK)])
        # 晚间黄瓜区雨水池已恢复正常 -> safe
        self.assertEqual(self.pick(res, 2), [(2, ss.SAFE)])
        # 仅在 05:30 从中部干管的机井切到雨水池 1 次
        self.assertEqual(res["summary"]["switch_count"], 1)
        sw = res["summary"]["switches"][0]
        self.assertEqual((sw["at"], sw["from_source_id"], sw["to_source_id"]),
                         ("05:30", 1, 2))

    def test_coverage_outranks_switch_count(self):
        # 即使切换会增加次数，也不能靠「不灌溉」拿到 0 切换：
        # 单阀门绑定风险机井，只登记安全清水塔且切换耗时 45 分钟，
        # 两轮灌溉相邻 08:00/08:30。第一轮可切（夜间空闲），
        # 第二轮不能切回机井（0 空闲），但可继续用清水塔（同水源不算切换）。
        sg = segs({1: WELL_RISK, 3: SAFE_WELL})
        zones = [{"id": 1, "name": "A", "valve_id": 1},
                 {"id": 2, "name": "B", "valve_id": 1}]
        items = [{"zone_id": 1, "start_slot": 32, "length": 2},
                 {"zone_id": 2, "start_slot": 34, "length": 2}]
        vs = reg({1: [{"source_id": 1, "switch_min": 0},
                      {"source_id": 3, "switch_min": 45}]})
        res = solve(zones, items,
                    [{"id": 1, "name": "V", "flow_lmin": 100, "source_id": 1}],
                    sg, DEFAULT_META, vs)
        self.assertTrue(res["summary"]["feasible"])
        self.assertEqual(res["summary"]["risk_l"], 0)
        self.assertEqual(res["summary"]["switch_count"], 1)

    def test_switch_back_requires_idle(self):
        # 清水塔容量只够一轮：第二轮若切回机井需要 45 分钟空闲，
        # 08:30 紧邻 08:30 -> 不允许，被迫使用风险机井（机井切换耗时 0 仅在
        # 与最近用水不同水源且空闲满足时；这里最近用水是清水塔）。
        sg = segs({1: WELL_RISK, 3: SAFE_WELL})
        zones = [{"id": 1, "name": "A", "valve_id": 1},
                 {"id": 2, "name": "B", "valve_id": 1}]
        items = [{"zone_id": 1, "start_slot": 32, "length": 2},
                 {"zone_id": 2, "start_slot": 34, "length": 2}]
        vs = reg({1: [{"source_id": 1, "switch_min": 0},
                      {"source_id": 3, "switch_min": 45}]})
        m = meta({1: {"name": "机井", "day_cap_l": 100000, "same_concur": 1},
                  3: {"name": "清水塔", "day_cap_l": 3000, "same_concur": 1}})
        res = solve(zones, items,
                    [{"id": 1, "name": "V", "flow_lmin": 100, "source_id": 1}],
                    sg, m, vs)
        # A 清水塔安全 3000 L（用尽容量），B 只能风险机井 3000 L
        self.assertEqual(res["summary"]["uncovered_l"], 0)
        self.assertEqual(res["summary"]["risk_l"], 3000.0)
        self.assertEqual(res["summary"]["switch_count"], 2)


class CapacityAndConcurrencyTests(unittest.TestCase):

    def test_day_cap_blocks_and_reports_conflict(self):
        sg = segs({2: POOL})
        zones = [{"id": 1, "name": "A", "valve_id": 1}]
        items = [{"zone_id": 1, "start_slot": 72, "length": 2}]  # 18:00 安全
        vs = reg({1: [{"source_id": 2, "switch_min": 0}]})
        m = meta({2: {"name": "雨水池", "day_cap_l": 1000, "same_concur": 1}})
        valves = [{"id": 1, "name": "V", "flow_lmin": 100, "source_id": None}]
        res = solve(zones, items, valves, sg, m, vs)
        self.assertFalse(res["summary"]["feasible"])
        self.assertEqual(res["summary"]["uncovered_l"], 3000.0)
        self.assertTrue(any("日供水上限" in r
                            for c in res["conflicts"] for r in c["reasons"]))

    def test_same_source_concurrency_half_open(self):
        sg = segs({2: POOL})
        zones = [{"id": 1, "name": "A", "valve_id": 1},
                 {"id": 2, "name": "B", "valve_id": 2}]
        # 两个不同阀门、不同时段但相接：[08:00,08:30) 与 [08:30,09:00)
        items = [{"zone_id": 1, "start_slot": 32, "length": 2},
                 {"zone_id": 2, "start_slot": 34, "length": 2}]
        valves = [{"id": 1, "name": "V1", "flow_lmin": 100, "source_id": 2},
                  {"id": 2, "name": "V2", "flow_lmin": 100, "source_id": 2}]
        vs = reg({1: [{"source_id": 2, "switch_min": 0}],
                  2: [{"source_id": 2, "switch_min": 0}]})
        m = meta({2: {"name": "雨水池", "day_cap_l": 100000, "same_concur": 1}})
        res = solve(zones, items, valves, sg, m, vs)
        # 相接不算并发，两路都能覆盖
        self.assertTrue(res["summary"]["feasible"])
        self.assertEqual(res["sources"][0]["max_concurrent"], 1)

        # 真正重叠 [08:00,08:30) 两路 -> 并发 1 拒绝其一
        items_overlap = [
            {"zone_id": 1, "start_slot": 32, "length": 2},
            {"zone_id": 2, "start_slot": 32, "length": 2}]
        res2 = solve(zones, items_overlap, valves, sg, m, vs)
        self.assertFalse(res2["summary"]["feasible"])
        self.assertEqual(res2["summary"]["uncovered_l"], 3000.0)
        self.assertTrue(any("同源并发" in r
                            for c in res2["conflicts"] for r in c["reasons"]))

    def test_no_registered_source_is_uncovered(self):
        sg = segs({1: WELL_RISK})
        zones = [{"id": 1, "name": "A", "valve_id": 1}]
        items = [{"zone_id": 1, "start_slot": 32, "length": 2}]
        valves = [{"id": 1, "name": "V", "flow_lmin": 100, "source_id": 1}]
        res = ss.solve(zones, items, valves, sg, DEFAULT_META,
                       ss.normalize_valve_sources({}, {1, 2, 3}))
        self.assertFalse(res["summary"]["feasible"])
        self.assertEqual(res["conflicts"][0]["reasons"],
                         ["阀门未登记任何可用水源"])


class RecalculateTests(unittest.TestCase):

    def test_sample_change_changes_risk(self):
        # 机井补两条恢复正常的采样 -> 风险消失
        recovered = WELL_RISK + [
            sample(7, 1, "2026-10-02T09:30:00", nitrate=25),
            sample(8, 1, "2026-10-02T10:00:00", nitrate=26),
        ]
        zones = [{"id": 1, "name": "番茄", "valve_id": 1}]
        items = [{"zone_id": 1, "start_slot": 40, "length": 4}]  # 10:00-11:00
        valves = [{"id": 1, "name": "北", "flow_lmin": 25, "source_id": 1}]
        vs = reg({1: [{"source_id": 1, "switch_min": 0}]})
        before = solve(zones, items, valves, segs({1: WELL_RISK}),
                       DEFAULT_META, vs)
        after = solve(zones, items, valves, segs({1: recovered}),
                      DEFAULT_META, vs)
        self.assertEqual(before["summary"]["risk_l"], 1500.0)
        self.assertEqual(after["summary"]["risk_l"], 0)
        self.assertEqual(after["summary"]["safe_l"], 1500.0)

    def test_capacity_change_changes_assignment(self):
        zones = [{"id": 1, "name": "黄瓜", "valve_id": 2}]
        items = [{"zone_id": 1, "start_slot": 72, "length": 6}]
        valves = [{"id": 2, "name": "中", "flow_lmin": 25, "source_id": 1}]
        vs = reg({2: [{"source_id": 1, "switch_min": 0},
                      {"source_id": 2, "switch_min": 20}]})
        sg = segs({1: WELL_RISK, 2: POOL})
        tight = solve(zones, items, valves, sg,
                      meta({1: {"name": "机井", "day_cap_l": 8000, "same_concur": 2},
                            2: {"name": "雨水池", "day_cap_l": 100, "same_concur": 2}}),
                      vs)
        loose = solve(zones, items, valves, sg,
                      meta({1: {"name": "机井", "day_cap_l": 8000, "same_concur": 2},
                            2: {"name": "雨水池", "day_cap_l": 5000, "same_concur": 2}}),
                      vs)
        # 容量 100 L 装不下 2250 L -> 只能风险机井；放宽后 -> 雨水池安全
        self.assertEqual(tight["summary"]["risk_l"], 2250.0)
        self.assertEqual(loose["summary"]["safe_l"], 2250.0)


class NormalizeTests(unittest.TestCase):

    def test_valve_sources_validation(self):
        with self.assertRaises(ValueError):
            ss.normalize_valve_sources(
                {1: [{"source_id": 9, "switch_min": 10}]}, {1, 2})
        with self.assertRaises(ValueError):
            ss.normalize_valve_sources(
                {1: [{"source_id": 1, "switch_min": -5}]}, {1, 2})
        with self.assertRaises(ValueError):
            ss.normalize_valve_sources(
                {1: [{"source_id": 1}, {"source_id": 1}]}, {1, 2})
        out = ss.normalize_valve_sources(
            {2: [{"source_id": 1, "switch_min": 30}]}, {1, 2})
        self.assertEqual(out[2][0]["switch_min"], 30)

    def test_source_meta_validation(self):
        with self.assertRaises(ValueError):
            ss.normalize_source_meta(
                {1: {"name": "井", "day_cap_l": -1, "same_concur": 1}})
        with self.assertRaises(ValueError):
            ss.normalize_source_meta(
                {1: {"name": "井", "day_cap_l": 100, "same_concur": 0}})
        out = ss.normalize_source_meta(
            {1: {"name": "井", "day_cap_l": 8000, "same_concur": 2}})
        self.assertEqual(out[1]["day_cap_l"], 8000.0)


if __name__ == "__main__":
    unittest.main()
