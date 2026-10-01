/* 水质异常替代水源方案 —— 原生 JS */
(() => {
  "use strict";

  const CLASS_TEXT = {
    risk: "风险", review: "待复核", safe: "正常",
    insufficient: "证据不足", uncovered: "未覆盖",
  };
  const CLASS_RANK = { risk: 0, uncovered: 1, insufficient: 2, review: 3, safe: 4 };
  const SOURCE_COLORS = ["#1565c0", "#00897b", "#6d4c41", "#5e35b1",
                         "#455a64", "#d84315"];

  let state = null;
  let currentDate = "";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ------------------------------------------------------------ 工具

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 3200);
  }

  async function api(path, options = {}) {
    const opts = { headers: {}, ...options };
    if (opts.body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) throw new Error((data && data.error) || `请求失败 (${res.status})`);
    return data;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return "—";
    const n = Math.round(Number(v) * 100) / 100;
    return n.toString();
  }
  function hm(ts) { return String(ts || "").slice(11, 16); }
  function tsToMin(ts) {
    const h = Number(ts.slice(11, 13));
    const mm = Number(ts.slice(14, 16));
    return h * 60 + mm;
  }
  function sourceColor(sid) {
    return "s" + ((((Number(sid) - 1) % SOURCE_COLORS.length)
      + SOURCE_COLORS.length) % SOURCE_COLORS.length + 1);
  }
  function colorHex(sid) {
    return SOURCE_COLORS[((Number(sid) - 1) % SOURCE_COLORS
      + SOURCE_COLORS.length) % SOURCE_COLORS.length];
  }
  function badge(klass) {
    return `<span class="trace-badge ${klass}">${CLASS_TEXT[klass] || klass}</span>`;
  }
  function sourcePill(name, sid) {
    if (!sid) return "—";
    return `<span class="src-pill ${sourceColor(sid)}">${esc(name)}</span>`;
  }

  // ------------------------------------------------------------ 加载

  function initialDate() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  async function load(date) {
    const url = "/api/water/switch/state"
      + (date ? `?date=${encodeURIComponent(date)}` : "");
    state = await api(url);
    currentDate = state.date;
    $("#plan-date").value = state.date;
    renderAll();
  }

  function renderAll() {
    renderSourceCaps();
    renderValveConfigs();
    renderVerdict();
    renderSummary();
    renderConflicts();
    renderTimeline();
    renderGroups();
    renderSourceRows();
    renderZoneRows();
  }

  // ------------------------------------------------------------ 水源容量

  function renderSourceCaps() {
    const ul = $("#source-list");
    ul.innerHTML = "";
    for (const s of state.source_cards) {
      const li = document.createElement("li");
      li.className = "src-cap-row";
      li.innerHTML = `
        <div class="src-cap-name">
          <i style="display:inline-block;width:10px;height:10px;border-radius:3px;
             background:${colorHex(s.id)}"></i>${esc(s.name)}
        </div>
        <div class="src-cap-fields">
          <label>日上限(L)
            <input type="number" min="0" step="1" data-cap
              value="${s.daily_cap_l ?? ""}" placeholder="不限">
          </label>
          <label>并发
            <input type="number" min="1" step="1" data-concur
              value="${s.source_concur}">
          </label>
          <button class="btn small primary" data-save-cap>保存</button>
        </div>`;
      li.querySelector("[data-save-cap]").addEventListener("click", async () => {
        const cap = li.querySelector("[data-cap]").value.trim();
        const concur = li.querySelector("[data-concur]").value.trim();
        try {
          state = await api(`/api/water/sources/${s.id}/limits`, {
            method: "PUT",
            body: { daily_cap_l: cap, source_concur: concur || "1" },
          });
          renderAll();
          toast("水源容量已更新，方案已重新核算");
        } catch (e) { toast(e.message, true); }
      });
      ul.appendChild(li);
    }
  }

  // ------------------------------------------------------------ 阀门候选

  async function saveCandidates(vid, rows) {
    try {
      state = await api(`/api/valves/${vid}/water-sources`, {
        method: "PUT", body: { sources: rows },
      });
      renderAll();
      toast("阀门可用水源已更新，方案已重新核算");
    } catch (e) { toast(e.message, true); }
  }

  function renderValveConfigs() {
    const root = $("#valve-config");
    root.innerHTML = "";
    for (const v of state.valve_cards) {
      const block = document.createElement("div");
      block.className = "valve-block";
      const candIds = v.candidates.map((c) => c.source_id);
      block.innerHTML = `
        <h3>${esc(v.valve_name)}
          <small>绑定：${v.bound_source_name ? esc(v.bound_source_name) : "（未绑定）"}
            · ${fmtNum(v.flow_lmin)} L/min</small></h3>
        <ul class="cand-list"></ul>
        <div class="add-cand">
          <select data-add-source>
            <option value="">添加可用水源…</option>
            ${state.source_cards.map((s) =>
              `<option value="${s.id}"${candIds.includes(s.id) ? " disabled" : ""}>${esc(s.name)}</option>`
            ).join("")}
          </select>
          <label>耗时(分)
            <input type="number" min="0" step="1" value="30" data-add-min
              style="width:58px;font:inherit;font-size:12px;padding:4px 6px;
                     border:1px solid var(--line);border-radius:6px">
          </label>
          <button class="btn small ghost" data-add-btn>添加</button>
        </div>`;
      const ul = block.querySelector(".cand-list");

      const collect = () => $$(".cand-row", ul).map((li) => ({
        source_id: Number(li.dataset.sid),
        switch_minutes: Math.max(
          0, parseInt(li.querySelector("input").value, 10) || 0),
      }));

      for (const c of v.candidates) {
        const li = document.createElement("li");
        li.className = "cand-row";
        li.dataset.sid = c.source_id;
        li.innerHTML = `
          <span class="cand-name">
            <i style="display:inline-block;width:9px;height:9px;border-radius:3px;
               background:${colorHex(c.source_id)}"></i>
            ${esc(c.source_name || `#${c.source_id}`)}
            ${c.is_bound ? '<span class="bound-tag">绑定</span>' : ""}
          </span>
          <input type="number" min="0" step="1" value="${c.switch_minutes}"
                 title="切换耗时（分钟）">
          <button class="del-x" title="移除该水源"
            ${c.is_bound ? 'style="visibility:hidden"' : ""}>✕</button>`;
        li.querySelector("input").addEventListener("change",
          () => saveCandidates(v.valve_id, collect()));
        const del = li.querySelector(".del-x");
        if (!c.is_bound) {
          del.addEventListener("click",
            () => saveCandidates(v.valve_id,
              collect().filter((r) => r.source_id !== c.source_id)));
        }
        ul.appendChild(li);
      }

      block.querySelector("[data-add-btn]").addEventListener("click", () => {
        const sel = block.querySelector("[data-add-source]");
        const min = block.querySelector("[data-add-min]").value;
        const sid = Number(sel.value);
        if (!sid) { toast("请先选择水源", true); return; }
        const rows = collect();
        if (rows.some((r) => r.source_id === sid)) { toast("已登记", true); return; }
        rows.push({ source_id: sid, switch_minutes: Math.max(0, parseInt(min, 10) || 0) });
        saveCandidates(v.valve_id, rows);
      });
      root.appendChild(block);
    }
  }

  // ------------------------------------------------------------ 结论 / 汇总

  function renderVerdict() {
    const el = $("#verdict-badge");
    if (state.feasible) {
      el.className = "verdict feasible";
      el.textContent = "✅ 存在可行方案（灌溉时刻不变）";
    } else {
      el.className = "verdict infeasible";
      el.textContent = "⚠️ 无完全可行方案：存在未覆盖水量";
    }
    const sv = $("#saved-state");
    if (state.saved) {
      const at = esc(state.saved.updated.slice(0, 16).replace("T", " "));
      sv.innerHTML = (state.saved.stale
        ? `<span class="stale">已保存方案已过期（采样 / 绑定 / 容量 / 计划已变更）· ${at}</span>`
        : `<span class="saved-ok">方案已保存 · ${at}</span>`)
        + ` <button class="btn small" id="btn-export-saved"
             style="border:1px solid var(--line);background:#fff;color:var(--ink)">
             ⬇ 导出已保存 JSON</button>`;
      $("#btn-export-saved").addEventListener("click", exportSaved);
    } else {
      sv.textContent = "尚未保存";
    }
  }

  function renderSummary() {
    const s = state.summary;
    const chips = [
      ["chip-safe", `总用水 ${fmtNum(s.total_l)} L`],
      ["chip-risk", `风险 ${fmtNum(s.risk_l)} L`],
      ["chip-insufficient", `证据不足 ${fmtNum(s.insufficient_l)} L`],
      ["chip-review", `待复核 ${fmtNum(s.review_l)} L`],
      ["chip-uncovered", `未覆盖 ${fmtNum(s.uncovered_l)} L`],
      ["chip-switch", `切换 ${s.switches} 次`],
    ];
    $("#summary-chips").innerHTML = chips
      .map(([cls, txt]) => `<span class="trace-chip ${cls}">${txt}</span>`).join("");
    $("#objective-hint").textContent =
      `优化目标：风险 + 证据不足 = ${fmtNum(s.objective_l)} L（最小化），`
      + `切换次数 ${s.switches}（最少）。`
      + (state.exhaustive ? ""
        : " 注：搜索预算耗尽，当前为可得的最好近似方案。");
  }

  // ------------------------------------------------------------ 冲突

  function renderConflicts() {
    const card = $("#conflict-card");
    if (state.feasible || !state.uncovered.length) {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    $("#conflict-list").innerHTML = state.uncovered.map((u) => {
      const body = u.no_candidate
        ? `<p class="no-cand-note">该阀门未登记任何可用水源（含当前绑定水源），请先在左侧登记。</p>`
        : `<ul>${u.candidate_sources.map((c) => `
            <li><span class="ci-src">${esc(c.source_name)}</span>：
              ${c.reasons.length
                ? c.reasons.map((r) => `<div>${esc(r)}</div>`).join("")
                : '<span style="color:var(--green-dark)">单独评估可用'
                  + '（未被选中以换取更优的总体目标）</span>'}
            </li>`).join("")}</ul>`;
      return `<div class="conflict-item">
        <div class="ci-head">${esc(u.valve_name)} · ${u.zone_names.map(esc).join("、")}
          · ${hm(u.start)}–${hm(u.end)} · 未覆盖 ${fmtNum(u.water_l)} L</div>
        ${body}
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------ 时间轴

  function renderTimeline() {
    const byValve = new Map();
    for (const g of state.groups) {
      if (!byValve.has(g.valve_id)) {
        byValve.set(g.valve_id, { name: g.valve_name, rows: [] });
      }
      byValve.get(g.valve_id).rows.push(g);
    }
    let html = "";
    for (const v of state.valves) {
      const vv = byValve.get(v.valve_id);
      if (!vv) continue;
      html += `<div class="tl-valve"><span class="tl-valve-name">${esc(vv.name)}</span>
        <span class="tl-track">`;
      for (const g of vv.rows) {
        const sMin = tsToMin(g.start);
        const eMin = tsToMin(g.end);
        const lft = (sMin / 1440 * 100).toFixed(3);
        const w = Math.max(0.35, (eMin - sMin) / 1440 * 100).toFixed(3);
        const cls = g.source_id ? sourceColor(g.source_id) : "uncovered";
        const k = g.klass === "risk" ? " klass-risk"
          : g.klass === "insufficient" ? " klass-insufficient" : "";
        const title = `${g.zone_names.join("、")} ${hm(g.start)}-${hm(g.end)} `
          + `${g.source_name || "未覆盖"} ${CLASS_TEXT[g.klass]} ${fmtNum(g.water_l)}L`;
        html += `<div class="tl-block ${cls}${k}" style="left:${lft}%;width:${w}%"
          title="${esc(title)}"><span>${esc(g.source_name || "未覆盖")}</span></div>`;
      }
      html += `</span>
        <div class="tl-hour-row">
          ${[0, 3, 6, 9, 12, 15, 18, 21, 24]
            .map((h) => `<span>${String(h).padStart(2, "0")}:00</span>`).join("")}
        </div></div>`;
    }
    $("#timeline").innerHTML = html;

    const usedIds = [...new Set(state.groups
      .map((g) => g.source_id).filter(Boolean))];
    $("#timeline-legend").innerHTML = usedIds.map((sid) => {
      const srow = state.sources.find((s) => s.source_id === sid);
      return `<span><i style="background:${colorHex(sid)}"></i>${esc(
        srow ? srow.source_name : `#${sid}`)}</span>`;
    }).join("") + `
      <span><i style="background:repeating-linear-gradient(45deg,#f3e3f7 0 3px,#e4c2ee 3px 6px)"></i>未覆盖</span>
      <span>色块斜纹 = 该运行内含风险 / 证据不足水质子段</span>`;
  }

  // ------------------------------------------------------------ 运行组明细

  function renderGroups() {
    const tbody = $("#group-rows");
    if (!state.groups.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="8">当日没有已编排的灌溉运行</td></tr>`;
      return;
    }
    tbody.innerHTML = state.groups.map((g) => {
      let gap;
      if (!g.source_id) gap = "—";
      else if (g.switched_from) {
        gap = `<span class="switch-mark">${fmtNum(g.gap_minutes)} ≥ `
          + `${g.required_minutes} 分钟 ✓</span>`;
      } else gap = "—";
      return `<tr>
        <td>${esc(g.valve_name)}</td>
        <td>${g.zone_names.map(esc).join("、")}</td>
        <td class="num">${hm(g.start)}–${hm(g.end)}</td>
        <td>${sourcePill(g.source_name, g.source_id)}</td>
        <td>${badge(g.klass)}</td>
        <td class="num">${fmtNum(g.water_l)}</td>
        <td class="num">${gap}</td>
        <td class="num">${g.source_id ? `${g.required_minutes} 分钟` : "—"}</td>
      </tr>`;
    }).join("");
  }

  // ------------------------------------------------------------ 水源 / 分区

  function renderSourceRows() {
    $("#source-rows").innerHTML = state.sources.map((s) => `
      <tr>
        <td>${sourcePill(s.source_name, s.source_id)}</td>
        <td class="num">${fmtNum(s.used_l)}</td>
        <td class="num">${s.daily_cap_l === null ? "不限" : fmtNum(s.daily_cap_l)}</td>
        <td class="num">${s.cap_remaining_l === null ? "—" : fmtNum(s.cap_remaining_l)}</td>
        <td class="num">${s.source_concur}</td>
      </tr>`).join("");
  }

  function renderZoneRows() {
    const rows = [...state.zones].sort(
      (a, b) => CLASS_RANK[a.klass] - CLASS_RANK[b.klass]);
    $("#zone-rows").innerHTML = rows.length
      ? rows.map((z) => `
        <tr>
          <td>${esc(z.zone_name)}</td>
          <td>${badge(z.klass)}</td>
          <td class="num">${fmtNum(z.risk_l)}</td>
          <td class="num">${fmtNum(z.review_l)}</td>
          <td class="num">${fmtNum(z.insufficient_l)}</td>
          <td class="num">${fmtNum(z.uncovered_l)}</td>
          <td class="num">${fmtNum(z.safe_l)}</td>
          <td>${z.source_names.map(esc).join("、") || "—"}</td>
        </tr>`).join("")
      : `<tr class="empty-row"><td colspan="8">当日没有已编排分区</td></tr>`;
  }

  // ------------------------------------------------------------ 保存 / 导出

  async function savePlan() {
    try {
      await api("/api/water/switch/plans", {
        method: "POST", body: { date: currentDate },
      });
      await load(currentDate);
      toast("方案已保存");
    } catch (e) { toast(e.message, true); }
  }

  function exportLive() {
    window.location.href =
      `/api/water/switch/export?date=${encodeURIComponent(currentDate)}`;
  }
  function exportSaved() {
    window.location.href =
      `/api/water/switch/export?saved=1&date=${encodeURIComponent(currentDate)}`;
  }

  // ------------------------------------------------------------ 事件

  $("#btn-recalc").addEventListener("click", async () => {
    try {
      await load($("#plan-date").value || initialDate());
      toast("已重新核算");
    } catch (e) { toast(e.message, true); }
  });
  $("#plan-date").addEventListener("change",
    () => load($("#plan-date").value).catch((e) => toast(e.message, true)));
  $("#btn-save").addEventListener("click", savePlan);
  $("#btn-export").addEventListener("click", exportLive);

  load(initialDate()).catch((e) => toast(e.message, true));
})();
