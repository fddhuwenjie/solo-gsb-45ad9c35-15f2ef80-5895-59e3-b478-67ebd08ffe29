/* 水质异常替代水源方案 —— 原生 JS */
(() => {
  "use strict";

  const STATUS_TEXT = { normal: "正常", observing: "观察", exceeded: "超标" };
  const CLASS_TEXT = { risk: "风险", review: "待复核", safe: "正常", insufficient: "证据不足" };

  let plan = null;
  let filterDate = "";

  const $ = (sel, root = document) => root.querySelector(sel);

  // ------------------------------------------------------------ 工具

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 3000);
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

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => {
    const n = Math.round(Number(v || 0) * 100) / 100;
    return Number.isInteger(n) ? String(n) : String(n);
  };
  const evidenceText = (e) => {
    if (!e) return "";
    if (e.source_kind === "sample") return `状态：当日采样 ${String(e.sample_ts || "").replace("T", " ").slice(0, 16)}`;
    if (e.source_kind === "carry") return `状态：历史采样 ${String(e.sample_ts || "").replace("T", " ").slice(0, 16)}`;
    if (e.source_kind === "unassigned") return "未覆盖";
    return "状态：无历史样本（证据不足）";
  };

  // ------------------------------------------------------------ 加载

  async function load() {
    const p = new URLSearchParams();
    if (filterDate) p.set("date", filterDate);
    plan = await api("/api/water/switch-plan?" + p.toString());
    if (!filterDate) {
      filterDate = plan.date;
      $("#plan-date").value = plan.date;
    }
    renderAll();
  }

  function renderAll() {
    renderSourceCaps();
    renderValveSources();
    renderVerdict();
    renderPlanRows();
    renderUsage();
    renderConflicts();
  }

  // ------------------------------------------------------------ 水源容量

  function renderSourceCaps() {
    const ul = $("#source-cap-list");
    ul.innerHTML = "";
    if (!plan.sources.length) {
      ul.innerHTML = `<li class="empty-reg">暂无水源，请先在水质监测页添加</li>`;
      return;
    }
    plan.sources.forEach((s) => {
      const li = document.createElement("li");
      li.className = "cap-card";
      const pct = s.day_cap_l > 0
        ? Math.min(100, Math.round((s.used_l / s.day_cap_l) * 100)) : 0;
      const over = s.day_cap_l > 0 && s.used_l > s.day_cap_l + 1e-6;
      li.innerHTML = `
        <div class="cap-head">
          <span class="dot ${s.status || "normal"}"></span>
          <span class="name">${esc(s.source_name)}</span>
          <span class="badge ${s.status || "normal"}">${STATUS_TEXT[s.status] || "—"}</span>
        </div>
        <div class="cap-fields">
          <label>日供水上限 (L)
            <input type="number" min="0" step="1" data-sid="${s.source_id}" data-key="day_cap_l" value="${num(s.day_cap_l)}">
          </label>
          <label>同源并发 (路)
            <input type="number" min="1" step="1" data-sid="${s.source_id}" data-key="same_concur" value="${s.same_concur}">
          </label>
        </div>
        <div class="cap-bar ${over ? "over" : ""}"><i style="width:${pct}%"></i></div>
        <div class="cap-meta">本方案已用 ${num(s.used_l)} / ${num(s.day_cap_l)} L · 峰值并发 ${s.max_concurrent}/${s.same_concur} 路</div>`;
      li.querySelectorAll("input").forEach((inp) => {
        inp.addEventListener("change", () => saveCapacity(s));
      });
      ul.appendChild(li);
    });
  }

  async function saveCapacity(source) {
    const capInp = $(`input[data-sid="${source.source_id}"][data-key="day_cap_l"]`);
    const conInp = $(`input[data-sid="${source.source_id}"][data-key="same_concur"]`);
    try {
      await api(`/api/water/sources/${source.source_id}`, {
        method: "PUT",
        body: {
          name: source.source_name,
          day_cap_l: Number(capInp.value),
          same_concur: Number(conInp.value),
        },
      });
      await load();
      toast("容量已更新，方案已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 阀门可用水源

  function renderValveSources() {
    const box = $("#valve-source-editor");
    box.innerHTML = "";
    const sourceOptions = (cur) =>
      `<option value="">选择水源…</option>` +
      plan.sources.map((s) =>
        `<option value="${s.source_id}"${s.source_id === cur ? " selected" : ""}>${esc(s.source_name)}</option>`
      ).join("");

    plan.valve_sources.forEach((v) => {
      const card = document.createElement("div");
      card.className = "valve-card";
      const bound = v.bound_source_name ? esc(v.bound_source_name) : "<em>未绑定</em>";
      card.innerHTML = `
        <div class="vname"><span>${esc(v.valve_name)}</span><small>当前接通：${bound}</small></div>
        <div class="reg-rows"></div>
        <button class="icon-btn add" type="button">＋ 登记水源</button>`;
      const rowsBox = card.querySelector(".reg-rows");
      const drawRows = () => {
        rowsBox.innerHTML = "";
        if (!v.sources.length) {
          rowsBox.innerHTML = `<div class="empty-reg">尚未登记可用水源（该阀门各分区将无法供水）</div>`;
          return;
        }
        v.sources.forEach((r) => {
          const row = document.createElement("div");
          row.className = "reg-row";
          row.innerHTML = `
            <select>${sourceOptions(r.source_id)}</select>
            <span class="sw-wrap"><input type="number" min="0" max="1440" step="1" value="${r.switch_min}">分钟</span>
            <button class="icon-btn" type="button" title="移除">🗑</button>`;
          row.querySelector("select").addEventListener("change", (e) => {
            r.source_id = Number(e.target.value);
            r.source_name = plan.sources.find((s) => s.source_id === r.source_id)?.source_name;
            commit(v);
          });
          row.querySelector("input").addEventListener("change", (e) => {
            r.switch_min = Math.max(0, Number(e.target.value) || 0);
            e.target.value = r.switch_min;
            commit(v);
          });
          row.querySelector("button").addEventListener("click", () => {
            v.sources = v.sources.filter((x) => x !== r);
            commit(v);
          });
          rowsBox.appendChild(row);
        });
      };
      const commit = async (vv) => {
        const clean = vv.sources
          .filter((r) => r.source_id)
          .map((r) => ({ source_id: r.source_id, switch_min: r.switch_min }));
        try {
          await api(`/api/valves/${vv.valve_id}/sources`, {
            method: "PUT", body: { sources: clean },
          });
          await load();
          toast("可用水源登记已更新，方案已重算");
        } catch (e) { toast(e.message, true); await load(); }
      };
      card.querySelector("button.add").addEventListener("click", () => {
        const first = plan.sources[0];
        v.sources.push({ source_id: first ? first.source_id : null,
                         source_name: first ? first.source_name : null,
                         switch_min: 15 });
        drawRows();
        // 新增行立即聚焦选择
        const sel = rowsBox.lastElementChild?.querySelector("select");
        if (sel) sel.focus();
      });
      drawRows();
      box.appendChild(card);
    });
  }

  // ------------------------------------------------------------ 结论 / 汇总

  function renderVerdict() {
    const s = plan.summary;
    const el = $("#verdict");
    if (s.feasible) {
      el.className = "verdict ok";
      el.textContent = `✅ 已形成全覆盖替代方案：${num(s.covered_l)} / ${num(s.planned_l)} L 全部供水，共 ${s.switch_count} 次切换`;
    } else {
      el.className = "verdict bad";
      el.textContent = `⚠️ 存在未覆盖水量 ${num(s.uncovered_l)} L：硬约束无法满足，请查看下方冲突原因`;
    }
    const chips = [
      ["risk", `风险 ${num(s.risk_l)} L`],
      ["review", `待复核 ${num(s.review_l)} L`],
      ["insufficient", `证据不足 ${num(s.insufficient_l)} L`],
      ["safe", `正常 ${num(s.safe_l)} L`],
      ["", `未覆盖 ${num(s.uncovered_l)} L`],
    ];
    $("#summary-chips").innerHTML = chips.map(([k, t]) =>
      `<span class="trace-chip ${k ? "chip-" + k : ""}">${t}</span>`).join("") +
      (plan.saved ? `<span class="saved-tag">已保存方案（修订 ${plan.saved.plan_revision ?? "?"}）</span>` : "");

    const o = s.objective;
    $("#objective").textContent =
      `优化顺序（字典序）：未覆盖 ${num(o.uncovered_l)} → 风险 ${num(o.risk_l)} → 证据不足 ${num(o.insufficient_l)} → 切换 ${o.switch_count} 次` +
      (s.exact ? "" : "（已达搜索上限，结果为近似）");
  }

  // ------------------------------------------------------------ 方案表

  function renderPlanRows() {
    const tbody = $("#plan-rows");
    tbody.innerHTML = "";
    plan.zones.forEach((z) => {
      const tr = document.createElement("tr");
      tr.className = "klass-" + (z.uncovered_l > 0 ? "insufficient" : worstClass(z));
      const pieces = z.pieces.map((p) => `
        <div class="piece klass-${p.covered ? p.klass : "insufficient"}">
          <div class="p-top">
            <span class="p-src">${p.covered ? esc(p.source_name) : "<em>未覆盖</em>"}</span>
            <span class="p-time">${p.start}–${p.end}（${num(p.minutes)} 分钟）</span>
            <span class="badge trace-badge ${p.covered ? p.klass : "insufficient"}">${CLASS_TEXT[p.covered ? p.klass : "insufficient"]}</span>
            <span class="p-vol">${num(p.water_l)} L</span>
            ${p.triggers.length ? `<span class="cand-status">${esc(p.triggers.join("、"))}</span>` : ""}
          </div>
          <div class="p-ev">${esc(evidenceText(p.evidence))}</div>
        </div>`).join("");
      const switches = z.switches.length
        ? z.switches.map((sw) =>
            `<span class="switch-chip">${sw.at} 边界：${sw.from_source_name ? esc(sw.from_source_name) : "（初始接通）"} → <b>${esc(sw.to_source_name)}</b></span>`
          ).join("")
        : `<span class="switch-chip none">区内无切换${z.segments.length === 1 ? "（单一水源）" : ""}</span>`;
      tr.innerHTML = `
        <td><div class="zone-main">${esc(z.zone_name)}</div>
            <div class="zone-sub">${esc(z.valve_name)} · ${z.start}–${z.end}</div></td>
        <td><div class="piece-list">${pieces}</div></td>
        <td class="switch-cell">${switches}</td>
        <td class="vol-cell">
          覆盖 <b class="g">${num(z.covered_l)}</b> L<br>
          风险 <b class="r">${num(z.risk_l)}</b> · 待复核 <b class="w">${num(z.review_l)}</b><br>
          ${z.uncovered_l > 0 ? `未覆盖 <b class="x">${num(z.uncovered_l)}</b> L` : ""}
        </td>`;
      tbody.appendChild(tr);
    });
    if (!plan.zones.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="4">该日期没有已编排的轮灌计划，请先在轮灌编排台生成计划</td></tr>`;
    }
  }

  function worstClass(z) {
    if (z.risk_l > 0) return "risk";
    if (z.review_l > 0) return "review";
    if (z.insufficient_l > 0) return "insufficient";
    return "safe";
  }

  // ------------------------------------------------------------ 水源用量

  function renderUsage() {
    const tbody = $("#source-usage-rows");
    tbody.innerHTML = plan.sources.map((s) => {
      const pct = s.day_cap_l > 0
        ? Math.min(100, Math.round((s.used_l / s.day_cap_l) * 100)) : 0;
      const over = s.day_cap_l > 0 && s.used_l > s.day_cap_l + 1e-6;
      return `<tr>
        <td><span class="dot ${s.status || "normal"}"></span>${esc(s.source_name)}</td>
        <td><span class="badge ${s.status || "normal"}">${STATUS_TEXT[s.status] || "—"}</span>
            ${s.triggers.length ? `<span class="cand-status">${esc(s.triggers.join("、"))}</span>` : ""}</td>
        <td><span class="usage-bar ${over ? "over" : ""}"><i style="width:${pct}%"></i></span>
            ${num(s.used_l)} / ${num(s.day_cap_l)} L</td>
        <td>${num(s.remain_l)} L</td>
        <td>${s.max_concurrent} / ${s.same_concur} 路</td>
      </tr>`;
    }).join("");
  }

  // ------------------------------------------------------------ 冲突

  function renderConflicts() {
    const card = $("#conflict-card");
    const tbody = $("#conflict-rows");
    if (!plan.conflicts.length) {
      card.hidden = true;
      tbody.innerHTML = "";
      return;
    }
    card.hidden = false;
    tbody.innerHTML = plan.conflicts.map((c) => `
      <tr>
        <td>${esc(c.zone_name)}<div class="zone-sub">${esc(c.valve_name)}</div></td>
        <td>${c.start}–${c.end}（${num(c.minutes)} 分钟）</td>
        <td><b class="x">${num(c.water_l)} L</b></td>
        <td><ul class="reason-list">
          ${c.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}
          ${c.candidate_status.length ? `<li class="cand-status">候选水源水质：${esc(c.candidate_status.join("；"))}</li>` : ""}
        </ul></td>
      </tr>`).join("");
  }

  // ------------------------------------------------------------ 操作

  async function savePlan() {
    try {
      await api("/api/water/switch-plan/save", {
        method: "POST", body: { date: filterDate },
      });
      await load();
      toast(plan.summary.feasible ? "方案已保存" : "方案已保存（含未覆盖水量，请按冲突原因处理）");
    } catch (e) { toast(e.message, true); }
  }

  function exportPlan() {
    const p = new URLSearchParams();
    if (filterDate) p.set("date", filterDate);
    window.location.href = "/api/water/switch-plan/export?" + p.toString();
  }

  $("#btn-recalc").addEventListener("click", async () => {
    filterDate = $("#plan-date").value || "";
    try { await load(); toast("已按当前采样 / 绑定 / 容量 / 登记 / 计划重新核算"); }
    catch (e) { toast(e.message, true); }
  });
  $("#plan-date").addEventListener("change", () => {
    filterDate = $("#plan-date").value || "";
    load().catch((e) => toast(e.message, true));
  });
  $("#btn-save").addEventListener("click", savePlan);
  $("#btn-export").addEventListener("click", exportPlan);
  $("#btn-export2").addEventListener("click", exportPlan);

  // ------------------------------------------------------------ 启动
  load().catch((e) => toast(e.message, true));
})();
