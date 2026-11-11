/* 组合监测前端：表格（Tabulator）、图表（ECharts）、对话框表单（HTMX 提交）。
 *
 * - 页面上的 <div data-table="id"> 自动从 /api/v1/tables/id 取数并渲染
 * - 列格式（fmt）统一在 FORMATS 里定义一次，所有表格共用
 * - 资产分配条形图和持仓表用同一份数据（表格加载后发出 pm:data 事件）
 */
(() => {
  "use strict";

  const root = getComputedStyle(document.documentElement);
  const color = (name) => root.getPropertyValue(`--${name}`).trim();

  // ---------- 格式 ----------

  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const num = (v, digits = 6) => Number(v).toLocaleString("en-US", { maximumFractionDigits: digits });
  const money = (v) => (v == null ? "—" : usd.format(v));
  const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
  const spct = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const signClass = (v) => (v == null || Math.abs(v) < 1e-12 ? "" : v > 0 ? "up" : "down");
  const localTime = (iso) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("zh-CN", { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };

  const FORMATS = {
    text: (v) => (v == null || v === "" ? "—" : String(v)),
    money,
    local: (v, row) => (v == null ? "—" : `${num(v, Math.abs(v) >= 1000 ? 2 : 4)} ${row.currency || ""}`.trim()),
    number: (v) => (v == null ? "—" : num(v)),
    pct,
    spct,
    date: (v) => v || "—",
    datetime: (v) => (v ? localTime(v) : "—"),
  };
  const NUMERIC = new Set(["money", "local", "number", "pct", "spct"]);
  const STATUS = {
    ok: ["status-ok", "✓ 正常"],
    failed: ["status-bad", "✗ 失败"],
    running: ["status-warn", "… 运行中"],
    stale: ["status-warn", "⚠ 沿用上次"],
  };
  const ACTIONS = { edit_holding: "编辑", remove_holding: "删除", edit_cash: "编辑", undo: "撤销" };

  // ---------- 请求与提示 ----------

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
    return data;
  }

  function showFlash(message) {
    const box = document.getElementById("flash");
    box.innerHTML = `<p class="alert alert-bad" role="alert">${esc(message)}</p>`;
  }

  document.body.addEventListener("htmx:responseError", (e) => {
    let message = `请求失败（${e.detail.xhr.status}）`;
    try { message = JSON.parse(e.detail.xhr.responseText).error || message; } catch { /* 不是 JSON */ }
    showFlash(message);
  });
  document.body.addEventListener("htmx:sendError", () => showFlash("连不上服务器"));

  // ---------- 表格 ----------

  const tables = new Map();
  const measure = document.createElement("canvas").getContext("2d");
  const textWidth = (text, weight = 400) => {
    measure.font = `${weight} 13px ${getComputedStyle(document.body).fontFamily}`;
    return measure.measureText(text).width;
  };

  function plainText(c, v, row) {
    if (c.fmt === "link") return String(v ?? "—");
    if (c.fmt === "status") return (STATUS[v] || ["", v ?? "—"])[1];
    if (c.fmt === "actions") return (row.actions || []).map((a) => ACTIONS[a]).join("  ");
    return (FORMATS[c.fmt] || FORMATS.text)(v, row);
  }

  // 按内容量出每列最小宽度：放得下时按比例拉伸，放不下时横向滚动；长文本列封顶并允许换行
  function minWidths(specs, columns, rows) {
    specs.forEach((c, i) => {
      if (c.width) return;
      let w = textWidth(c.title, 700) + 44;  // 表头加粗，右侧留排序箭头
      for (const r of rows.slice(0, 300)) w = Math.max(w, textWidth(plainText(c, r[c.field], r)) * 1.08 + 24);  // 单元格用等宽数字，比测量的略宽
      columns[i].minWidth = Math.ceil(Math.min(w, c.grow ? 220 : 360));
    });
  }

  function column(c) {
    const col = {
      title: c.title, field: c.field, hozAlign: c.align, headerHozAlign: c.align,
      headerSort: c.fmt !== "actions", minWidth: 56,
    };
    if (c.width) col.width = c.width;
    if (c.grow) {
      col.widthGrow = c.grow;
      col.cssClass = "cell-wrap";
      col.variableHeight = true;
    }
    if (NUMERIC.has(c.fmt)) col.sorter = "number";
    col.formatter = (cell) => {
      const v = cell.getValue();
      const row = cell.getRow().getData();
      if (c.fmt === "link") return row.url ? `<a href="${esc(row.url)}">${esc(v ?? "—")}</a>` : esc(v ?? "—");
      if (c.fmt === "status") {
        const [cls, text] = STATUS[v] || ["", v ?? "—"];
        return `<span class="${cls}">${esc(text)}</span>`;
      }
      if (c.fmt === "actions") {
        return (row.actions || []).map((a) =>
          `<button type="button" class="btn-link${a === "remove_holding" ? " danger" : ""}" data-action="${a}">${ACTIONS[a]}</button>`
        ).join("");
      }
      const text = (FORMATS[c.fmt] || FORMATS.text)(v, row);
      const cls = c.color === "sign" ? signClass(v) : "";
      return cls ? `<span class="${cls}">${esc(text)}</span>` : esc(text);
    };
    if (c.fmt === "actions") col.cellClick = onAction;
    return col;
  }

  function tableURL(el) {
    const params = new URLSearchParams();
    if (el.dataset.groupBy) params.set("group_by", el.dataset.groupBy);
    if (el.dataset.accountId) params.set("account_id", el.dataset.accountId);
    return `/api/v1/tables/${el.dataset.table}?${params}`;
  }

  async function loadTable(el) {
    el.classList.add("loading");
    try {
      const payload = await getJSON(tableURL(el));
      const columns = payload.columns.map(column);
      minWidths(payload.columns, columns, payload.rows);
      if (columns.length > 3) columns[0].frozen = true;  // 窄屏横向滑动时第一列不动
      const sort = payload.sort ? [{ column: payload.sort[0], dir: payload.sort[1] }] : [];
      let table = tables.get(el);
      if (!table) {
        const many = payload.rows.length > 50;
        table = new Tabulator(el, {
          data: payload.rows, columns, layout: "fitColumns", placeholder: "暂无数据",
          initialSort: sort, columnDefaults: { resizable: false },
          pagination: many, paginationSize: 50, paginationCounter: many ? "rows" : undefined,
          locale: "zh-cn", langs: { "zh-cn": { pagination: { first: "首页", last: "末页", prev: "上一页", next: "下一页", counter: { showing: "", of: "/", rows: "行", pages: "页" } } } },
        });
        tables.set(el, table);
      } else {
        table.setColumns(columns);
        await table.replaceData(payload.rows);
        if (sort.length) table.setSort(sort);
      }
      el.dispatchEvent(new CustomEvent("pm:data", { detail: payload }));
    } catch (err) {
      el.innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
    } finally {
      el.classList.remove("loading");
    }
  }

  function onAction(e, cell) {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const row = cell.getRow().getData();
    const accountId = cell.getTable().element.dataset.accountId;
    switch (btn.dataset.action) {
      case "edit_holding":
        openDialog("dlg-holding", {
          mode: "edit",
          values: {
            symbol: row.instrument_id, name: row.name, asset_class: row.asset_class_value,
            quantity: row.quantity, price: row.price, cost_price: row.cost_price,
          },
        });
        break;
      case "remove_holding":
        if (window.confirm(`删除持仓 ${row.display_name}（${row.instrument_id}）？之后可以撤销。`)) {
          htmx.ajax("POST", `/accounts/${accountId}/edit`, {
            values: { kind: "remove_holding", instrument_id: row.instrument_id }, target: "#flash", swap: "innerHTML",
          });
        }
        break;
      case "edit_cash":
        openDialog("dlg-cash", { values: { currency: row.currency, amount: row.quantity } });
        break;
      case "undo":
        htmx.ajax("POST", `/changes/${row.id}/undo`, { values: { next: location.pathname }, target: "#flash", swap: "innerHTML" });
        break;
    }
  }

  // ---------- 对话框 ----------

  function syncToggles(scope) {
    scope.querySelectorAll("[data-toggle]").forEach((box) => {
      const target = document.getElementById(box.dataset.toggle);
      if (target) target.hidden = box.checked;
    });
  }

  function openDialog(id, { mode = "new", values = {} } = {}) {
    const dialog = document.getElementById(id);
    if (!dialog) return;
    const form = dialog.querySelector("form");
    form.reset();
    dialog.querySelector(".form-msg").innerHTML = "";
    for (const [name, value] of Object.entries(values)) {
      const field = form.elements[name];
      if (field && value != null) field.value = typeof value === "number" ? String(+value.toFixed(6)) : value;
    }
    const title = dialog.querySelector("[data-title-new]");
    if (title) title.textContent = mode === "edit" ? title.dataset.titleEdit : title.dataset.titleNew;
    if (form.elements.symbol) form.elements.symbol.readOnly = mode === "edit";
    syncToggles(form);
    dialog.showModal();
    form.querySelector("input:not([type=hidden]):not([readonly]):not([type=radio])")?.focus();
  }

  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open]");
    if (opener) openDialog(opener.dataset.open, { mode: opener.dataset.mode || "new" });
    const closer = e.target.closest("[data-close]");
    if (closer) closer.closest("dialog")?.close();
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-toggle]")) syncToggles(e.target.form || document);
  });

  // ---------- 分段切换 ----------

  document.querySelectorAll(".seg[data-control]").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-value]");
      if (!btn || btn.getAttribute("aria-pressed") === "true") return;
      seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      seg.dispatchEvent(new CustomEvent("pm:change", { detail: btn.dataset.value }));
    });
  });

  // ---------- 图表 ----------

  const axisText = () => ({ color: color("muted"), fontSize: 11 });
  const pctTick = (v) => `${(v * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;

  function tooltipBase() {
    return {
      backgroundColor: color("surface"), borderColor: color("grid"), borderWidth: 1, padding: [8, 10],
      textStyle: { color: color("ink"), fontSize: 12 }, extraCssText: "box-shadow:0 2px 8px rgba(0,0,0,.08)",
    };
  }

  function curveOption(data) {
    const pts = data.points;
    const xAxis = (grid, showLabels) => ({
      type: "time", gridIndex: grid, axisTick: { show: false },
      axisLine: { lineStyle: { color: color("axis") } },
      axisLabel: { ...axisText(), show: showLabels, hideOverlap: true }, splitLine: { show: false },
    });
    const yAxis = (grid, extra = {}) => ({
      type: "value", gridIndex: grid, axisLabel: { ...axisText(), formatter: pctTick },
      splitLine: { lineStyle: { color: color("grid") } }, axisLine: { show: false }, axisTick: { show: false }, ...extra,
    });
    const heading = (text, top) => ({ text, left: 0, top, textStyle: { fontSize: 12, fontWeight: 500, color: color("ink-2") } });
    return {
      animation: false,
      textStyle: { fontFamily: getComputedStyle(document.body).fontFamily },
      title: [heading("累计收益", 0), heading("回撤", "67%")],
      grid: [{ left: 52, right: 60, top: 26, height: "52%" }, { left: 52, right: 60, top: "75%", bottom: 22 }],
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: color("axis") } },
      tooltip: {
        ...tooltipBase(), trigger: "axis",
        formatter: (items) => {
          const p = pts[items[0].dataIndex];
          if (!p) return "";
          let html = `<b>${p.date}</b><br>累计 ${spct(p.cum_return)}<br>回撤 ${spct(p.drawdown)}<br>投资资产 ${money(p.nav)}`;
          if (p.net_flow) html += `<br>当日净入金 ${money(p.net_flow)}`;
          return html;
        },
      },
      xAxis: [xAxis(0, false), xAxis(1, true)],
      yAxis: [yAxis(0), yAxis(1, { max: 0 })],
      series: [
        {
          name: "累计收益", type: "line", xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, symbolSize: 8,
          data: pts.map((p) => [p.date, p.cum_return]),
          lineStyle: { width: 2, color: color("accent") }, itemStyle: { color: color("accent") },
          endLabel: { show: true, formatter: (p) => spct(p.value[1]), color: color("ink"), fontSize: 12 },
        },
        {
          name: "回撤", type: "line", xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, symbolSize: 8,
          data: pts.map((p) => [p.date, p.drawdown]),
          lineStyle: { width: 1.5, color: color("down") }, itemStyle: { color: color("down") },
          areaStyle: { color: color("down"), opacity: 0.12 },
        },
      ],
    };
  }

  function barRows(payload) {
    let rows = payload.rows
      .filter((r) => (r.market_value_usd || 0) > 0)
      .map((r) => ({ name: r.group ?? r.name ?? r.instrument_id, weight: r.weight, usd: r.market_value_usd }));
    if (rows.length > 10) {
      const rest = rows.slice(9);
      rows = rows.slice(0, 9);
      rows.push({
        name: `其他 ${rest.length} 项`,
        weight: rest.reduce((s, r) => s + r.weight, 0),
        usd: rest.reduce((s, r) => s + r.usd, 0),
      });
    }
    return rows;
  }

  function barOption(rows) {
    return {
      animation: false,
      textStyle: { fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { left: 0, right: 128, top: 4, bottom: 4, containLabel: true },
      xAxis: { type: "value", show: false, max: Math.max(...rows.map((r) => r.weight), 0.0001) },
      yAxis: {
        type: "category", inverse: true, data: rows.map((r) => r.name),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: color("ink-2"), fontSize: 12, width: 112, overflow: "truncate" },
      },
      tooltip: {
        ...tooltipBase(), trigger: "item",
        formatter: (p) => `${esc(p.name)}<br>${pct(rows[p.dataIndex].weight)} · ${money(rows[p.dataIndex].usd)}`,
      },
      series: [{
        type: "bar", data: rows.map((r) => r.weight), barWidth: 14, barCategoryGap: "40%",
        itemStyle: { color: color("accent"), borderRadius: [0, 4, 4, 0] },
        label: {
          show: true, position: "right", color: color("ink"), fontSize: 12,
          formatter: (p) => `${pct(rows[p.dataIndex].weight)}  ${money(rows[p.dataIndex].usd)}`,
        },
      }],
    };
  }

  function dashboard() {
    const curveEl = document.getElementById("curve-chart");
    const barsEl = document.getElementById("alloc-chart");
    const holdingsEl = document.getElementById("holdings-table");
    if (!curveEl || typeof echarts === "undefined") return;
    const curve = echarts.init(curveEl, null, { renderer: "svg", locale: "ZH" });
    const bars = echarts.init(barsEl, null, { renderer: "svg", locale: "ZH" });

    const loadCurve = async (range) => {
      curveEl.classList.add("loading");
      try {
        curve.setOption(curveOption(await getJSON(`/api/v1/curve?range=${encodeURIComponent(range)}`)), true);
      } catch (err) {
        showFlash(err.message);
      } finally {
        curveEl.classList.remove("loading");
      }
    };
    document.querySelector('[data-control="range"]').addEventListener("pm:change", (e) => loadCurve(e.detail));
    document.querySelector('[data-control="alloc"]').addEventListener("pm:change", (e) => {
      holdingsEl.dataset.groupBy = e.detail;
      loadTable(holdingsEl);
    });
    holdingsEl.addEventListener("pm:data", (e) => {
      const rows = barRows(e.detail);
      barsEl.style.height = `${rows.length * 32 + 12}px`;
      bars.resize();
      bars.setOption(barOption(rows), true);
    });
    window.addEventListener("resize", () => { curve.resize(); bars.resize(); });
    loadCurve("all");
  }

  dashboard();
  document.querySelectorAll("[data-table]").forEach(loadTable);
})();
