/* QForgeLab dashboard app -- vanilla ES5-compatible SPA, ECharts local. */
"use strict";

const PAGES = [
  ["overview", "平台总览", "◉"],
  ["research", "研究实验室", "⚗"],
  ["queue", "研究队列", "≡"],
  ["market", "行情与预测", "📈"],
  ["paper", "模拟盘交易", "✎"],
  ["events", "事件驱动", "⚡"],
  ["data", "数据健康", "⛁"],
  ["system", "系统管理", "⚙"],
];

const BADGE_MAP = {
  "PAPER": "准入模拟", "CANDIDATE": "候选策略", "RESEARCH": "研究中", "REJECT": "未准入",
  "ACTIVE": "运行中", "PAUSED": "已暂停", "STOPPED": "已停止",
  "SUCCEEDED": "成功", "FAILED": "失败", "RUNNING": "执行中", "PENDING": "排队中",
  "LOCKED": "锁定密封", "KNOWN": "已消耗公开", "CONSISTENT": "一致通过", "INCONSISTENT": "存在差异",
  "OK": "正常", "WARN": "警告", "BAD": "异常", "EMPTY": "无数据",
  "buy": "买入", "BUY": "买入", "sell": "卖出", "SELL": "卖出",
  "BUY_CANDIDATE": "买入候选", "SELL_CANDIDATE": "卖出候选",
  "HOLD": "继续持有", "NO_TRADE": "无需操作",
  "STRONG_BUY": "强烈看多",
};

const state = { charts: {}, page: null, marketCode: null, selBacktest: null, selFactor: null };

/* ---------------- helpers ---------------- */
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}
async function postApi(path, body = null) {
  const opts = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (body != null) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    let err = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.detail) err = j.detail; else if (j.error) err = j.error; } catch (_) {}
    throw new Error(err);
  }
  return r.json();
}
function toast(msg, type = "info") {
  let box = $("toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast-box";
    document.body.appendChild(box);
  }
  const ico = type === "success" ? "✓" : type === "error" ? "✕" : "ℹ";
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.innerHTML = `<span style="font-weight:bold">${ico}</span><span>${esc(msg)}</span>`;
  box.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    t.style.transform = "translateY(-10px)";
    setTimeout(() => t.remove(), 300);
  }, 3500);
}
const $ = (id) => document.getElementById(id);
function esc(s) { return s == null ? "" : String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function fmt(v, d = 2) { return v == null || isNaN(v) ? "–" : Number(v).toFixed(d); }
function pct(v, d = 1) { return v == null || isNaN(v) ? "–" : (v * 100).toFixed(d) + "%"; }
function cls(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
function badge(v) {
  if (v == null) return "";
  const raw = String(v).replace(/\s/g, "_");
  const text = BADGE_MAP[v] || BADGE_MAP[raw] || esc(v);
  return `<span class="badge ${esc(raw)}">${esc(text)}</span>`;
}
function el(tag, attrs = {}, html = "") { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); e.innerHTML = html; return e; }

/* ---------------- modal dialogs ---------------- */
function closeModal() {
  const mb = $("modal-box");
  if (mb) mb.innerHTML = "";
}
window.closeModal = closeModal;

function openAddPositionModal(defaultSid = "S1_DEFENSIVE_QUALITY") {
  const mb = $("modal-box");
  if (!mb) return;
  mb.innerHTML = `
    <div class="modal-overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal-card">
        <div class="modal-header">
          <h3>➕ 手动录入持仓股票 / 自选建仓</h3>
          <button class="modal-close" onclick="closeModal()">✕</button>
        </div>
        <div class="modal-body">
          <div class="form-group">
            <label>股票 / ETF 代码</label>
            <input id="inp-pos-code" placeholder="输入代码，如 600519、510300 或 000001.SZ" autofocus>
            <div class="form-tip">支持6位纯数字或带后缀代码，系统自动匹配交易所与名称</div>
          </div>
          <div class="form-group">
            <label>持仓股数 (股 / 份)</label>
            <input id="inp-pos-shares" type="number" step="100" min="1" placeholder="如 1000">
          </div>
          <div class="form-group">
            <label>持仓成本价 (元，选填)</label>
            <input id="inp-pos-price" type="number" step="0.01" min="0.01" placeholder="留空将自动获取最新历史收盘价">
          </div>
          <div class="form-group">
            <label>归属策略账户</label>
            <select id="inp-pos-sid">
              <option value="${esc(defaultSid)}">${esc(defaultSid)} (当前活跃账户)</option>
              <option value="S1_DEFENSIVE_QUALITY">S1_DEFENSIVE_QUALITY (防御质量)</option>
              <option value="S4_ETF_ROTATION">S4_ETF_ROTATION (ETF动量轮动)</option>
            </select>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" onclick="closeModal()">取消</button>
          <button id="btn-submit-pos" class="btn btn-success" onclick="submitAddPosition()">确认添加持仓</button>
        </div>
      </div>
    </div>
  `;
}
window.openAddPositionModal = openAddPositionModal;

async function submitAddPosition() {
  const code = ($("inp-pos-code").value || "").trim();
  const shares = parseFloat($("inp-pos-shares").value);
  const priceVal = $("inp-pos-price").value ? parseFloat($("inp-pos-price").value) : null;
  const sid = $("inp-pos-sid").value;

  if (!code) { toast("请输入有效的股票代码", "error"); return; }
  if (isNaN(shares) || shares <= 0) { toast("请输入大于0的持仓股数", "error"); return; }

  const btn = $("btn-submit-pos");
  if (btn) {
    btn.innerHTML = `<span class="spin">◐</span> 正在录入...`;
    btn.classList.add("btn-loading");
  }

  try {
    let url = `/api/actions/add_paper_position?ts_code=${encodeURIComponent(code)}&shares=${shares}&strategy_id=${encodeURIComponent(sid)}`;
    if (priceVal != null && !isNaN(priceVal) && priceVal > 0) {
      url += `&price=${priceVal}`;
    }
    const res = await postApi(url);
    toast(res.message || "持仓添加成功！", "success");
    closeModal();
    if (state.page === "paper") await pagePaper();
  } catch (e) {
    toast("添加持仓失败: " + e.message, "error");
  } finally {
    if ($("btn-submit-pos")) {
      $("btn-submit-pos").innerHTML = "确认添加持仓";
      $("btn-submit-pos").classList.remove("btn-loading");
    }
  }
}
window.submitAddPosition = submitAddPosition;

async function triggerRemovePosition(code, sid) {
  if (!confirm(`确定要清空 ${code} 的持仓吗？`)) return;
  try {
    const res = await postApi(`/api/actions/remove_paper_position?ts_code=${encodeURIComponent(code)}&strategy_id=${encodeURIComponent(sid)}`);
    toast(res.message || "持仓已清空", "success");
    if (state.page === "paper") await pagePaper();
  } catch (e) {
    toast("清仓失败: " + e.message, "error");
  }
}
window.triggerRemovePosition = triggerRemovePosition;

function chart(id, option) {
  let c = state.charts[id];
  const box = $(id);
  if (!box) return;
  if (c) { c.dispose(); }
  c = echarts.init(box, null, { renderer: "canvas" });
  c.setOption(option);
  state.charts[id] = c;
}
window.addEventListener("resize", () => Object.values(state.charts).forEach(c => c.resize()));

const AXIS = { axisLine: { lineStyle: { color: "#2c3745" } }, axisLabel: { color: "#8b98a9", fontSize: 10.5 },
               splitLine: { lineStyle: { color: "rgba(44,55,69,.45)" } } };
const TOOLTIP = { backgroundColor: "#161c24", borderColor: "#232c38", textStyle: { color: "#dbe2ea", fontSize: 12 } };

/* ---------------- routing ---------------- */
function nav(page) { location.hash = "#" + page; }
window.addEventListener("hashchange", () => render());

async function refreshPage() { await render(); }

async function render() {
  let page = (location.hash || "#overview").slice(1);
  if (!PAGES.some(p => p[0] === page)) page = "overview";
  state.page = page;
  for (const c of Object.values(state.charts)) c.dispose();
  state.charts = {};
  $("page-title").textContent = PAGES.find(p => p[0] === page)[1];
  document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.page === page));
  $("page").innerHTML = `<div class="empty">加载中 <span class="spin">◐</span></div>`;
  const fn = { overview: pageOverview, research: pageResearch, queue: pageQueue, market: pageMarket,
               paper: pagePaper, events: pageEvents, data: pageData, system: pageSystem }[page];
  try { await fn(); } catch (e) { $("page").innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message)}</div></div>`; }
}

/* ---------------- Overview ---------------- */
async function pageOverview() {
  const d = await api("/api/overview");
  $("foot-version").textContent = "版本号 v" + d.version;
  const rs = d.runtime || {};
  const runState = rs.status === "RUNNING" ? "正在运行" : (rs.status === "IDLE" ? "空闲待命" : "未启动");
  const barsDate = d.bars.max_date || "–";
  const paper = d.paper || [];
  const totalNav = paper.reduce((a, p) => a + (p.nav || 0), 0);
  const stages = d.strategy_stages || {};
  const lastOpen = d.calendar.last_open || "–";
  $("page").innerHTML = `
  <div class="action-banner">
    <div>
      <div class="banner-text">⚡ 快速量化预测与模拟盘控制台</div>
      <div class="banner-sub">一键触发全市场量化模型预测与模拟盘盯市，自动计算明日推荐买入/卖出标的与仓位</div>
    </div>
    <div style="display:flex;gap:10px">
      <button id="btn-top-paper" class="btn btn-primary" onclick="triggerPaperStep()">⚡ 运行今日预测</button>
      <button class="btn ghost" onclick="nav('paper')">查看模拟持仓 →</button>
    </div>
  </div>
  <div class="grid c4">
    ${kpiCard("后台守护状态", runState, rs.ts ? "心跳时间 " + esc(rs.ts.slice(11, 19)) : "守护进程未启动")}
    ${kpiCard("精选行情数据", barsDate, `${(d.bars.rows / 1e4).toFixed(1)} 万条 curated bars`)}
    ${kpiCard("标的宇宙覆盖", (d.universe.days || 0) + " 个交易日", `覆盖 ${(d.universe.rows / 1e4).toFixed(0)} 万条截面 · 生产池 ${d.universe.production_eligible_securities} 只`)}
    ${kpiCard("模拟盘资产规模", paper.length ? "¥" + (totalNav / 1e4).toFixed(1) + " 万元" : "待启用", paper.length + " 个策略账户 · 模拟交易")}
  </div>
  <div class="grid c4" style="margin-top:14px">
    ${kpiCard("准入模拟 (PAPER)", stages.PAPER || 0, "通过闸门考验的优秀策略")}
    ${kpiCard("候选策略 (CANDIDATE)", stages.CANDIDATE || 0, "有模拟盘实盘跟踪业绩")}
    ${kpiCard("研究中 (RESEARCH)", stages.RESEARCH || 0, "正在研发与回测检验")}
    ${kpiCard("未准入 (REJECT)", stages.REJECT || 0, "未达到风控门槛被拒绝")}
  </div>
  <div class="grid c2" style="margin-top:14px">
    <div class="card"><h3>今日任务调度流（最新交易日）</h3>
      <table class="tbl"><tr><th>任务名称</th><th>业务日期</th><th>运行状态</th></tr>
      ${(d.runs || []).map(r => `<tr><td class="mono">${esc(r.job)}</td><td class="mono">${esc(r.business_date)}</td>
        <td>${badge(r.status)}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">尚无任务记录</td></tr>'}
      </table></div>
    <div class="card"><h3>最近系统事件与告警</h3>
      ${(d.alerts || []).slice(0, 8).map(a => `<div class="gate"><span>${badge(a.level)} ${esc(a.source)}</span>
        <span class="muted mono">${esc((a.message || "").slice(0, 60))}</span></div>`).join("") || '<div class="empty">无告警</div>'}
      <h3 style="margin-top:14px">样本外测试数据 (OOS) 密封注册表</h3>
      <table class="tbl"><tr><th>数据集分类</th><th>样本测试窗口</th><th>锁定状态</th></tr>
      ${(d.oos || []).map(o => `<tr><td class="mono">${esc(o.scope)}</td>
        <td class="mono">${esc(o.start)} ~ ${esc(o.end)}</td><td>${badge(o.status)}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">未初始化</td></tr>'}
      </table>
    </div>
  </div>
  <div class="note">安全声明：系统严格限定为模拟研究环境 · 禁止实盘外发 · 无券商实盘接口 · 无资金账户访问权限 · 仅限本地 127.0.0.1 访问。</div>`;
  $("top-status").innerHTML = `<span style="margin-right:10px">交易日 ${lastOpen}</span>
    <button class="btn btn-primary btn-sm" onclick="triggerPaperStep()">⚡ 运行今日预测</button>`;
}

function kpiCard(title, value, sub) {
  return `<div class="card"><h3>${esc(title)}</h3><div class="kpi-value">${esc(value)}</div><div class="kpi-sub">${esc(sub)}</div></div>`;
}

/* ---------------- Research Lab ---------------- */
async function pageResearch() {
  const [strats, factors, evals] = await Promise.all([
    api("/api/research/strategies"), api("/api/research/factors"), api("/api/research/evaluations")]);
  $("page").innerHTML = `
    <div class="sec-title">策略注册表与最新评级（稳健性 > 样本外验证 > 风险控制 > 收益率）</div>
    <div class="card"><div class="scroll"><table class="tbl" id="strat-tbl">
      <tr><th>策略代码</th><th>策略类别</th><th>准入评级</th><th class="num">夏普比率</th><th class="num">年化收益</th>
          <th class="num">最大回撤</th><th class="num">OOS验证</th><th>最新评估时间</th></tr>
      ${strats.map(s => {
        const m = (s.latest_eval || {}).metrics || {};
        return `<tr class="clickable" data-sid="${esc(s.strategy_id)}">
          <td class="mono">${esc(s.strategy_id)}</td><td>${esc(s.category)}</td>
          <td>${badge(s.status)}</td>
          <td class="num ${cls(m.sharpe)}">${fmt(m.sharpe)}</td>
          <td class="num ${cls(m.cagr)}">${pct(m.cagr)}</td>
          <td class="num ${cls(m.mdd)}">${pct(m.mdd)}</td>
          <td class="num">${s.latest_eval && s.latest_eval.uses_oos ? "✓ 通过" : "–"}</td>
          <td class="muted">${esc((s.latest_eval || {}).created_at || "–")}</td></tr>`;
      }).join("")}</table></div></div>
    <div class="sec-title">因子注册表（RankIC · 5分位分层 · 多空单调性）</div>
    <div class="card"><div class="scroll"><table class="tbl" id="factor-tbl">
      <tr><th>因子代码</th><th>所属类别</th><th>因子经济学说明</th><th class="num">RankIC均值</th><th class="num">信息比(ICIR)</th>
          <th class="num">t检验值</th><th class="num">多空年化收益</th><th>收益单调性</th></tr>
      ${factors.map(f => `<tr class="clickable" data-fid="${esc(f.factor_id)}">
        <td class="mono">${esc(f.factor_id)}</td><td>${esc(f.category)}</td>
        <td class="muted" style="white-space:normal;min-width:260px">${esc(f.description)}</td>
        <td class="num ${cls(f.ic_mean)}">${fmt(f.ic_mean, 3)}</td>
        <td class="num ${cls(f.ic_ir)}">${fmt(f.ic_ir, 2)}</td>
        <td class="num">${fmt(f.ic_tstat, 1)}</td>
        <td class="num ${cls(f.long_short_ann)}">${pct(f.long_short_ann)}</td>
        <td>${f.monotonics == null ? "–" : (f.monotonics ? "✓ 单调" : "·")}</td></tr>`).join("")}
      </table></div></div>
    <div id="bt-detail"></div><div id="factor-detail"></div>`;
  document.querySelectorAll("#strat-tbl tr.clickable").forEach(tr =>
    tr.onclick = () => selectBacktest(tr.dataset.sid));
  document.querySelectorAll("#factor-tbl tr.clickable").forEach(tr =>
    tr.onclick = () => selectFactor(tr.dataset.fid));
  state._strats = strats;
}

async function selectBacktest(sid) {
  const bts = await api("/api/research/backtests?strategy_id=" + sid);
  if (!bts.length) { $("bt-detail").innerHTML = `<div class="card empty">该策略暂无回测记录</div>`; return; }
  const main = bts.find(b => b.scope === "research") || bts[0];
  const d = await api("/api/research/backtest/" + main.bt_id);
  const nav = d.nav;
  const dates = nav.map(r => r.trade_date);
  const mkChart = () => {
    chart("ch-nav", { tooltip: { ...TOOLTIP, trigger: "axis" }, legend: { data: ["策略净值", "基准(沪深300)"], textStyle: { color: "#8b98a9" }, top: 0 },
      grid: { left: 55, right: 20, top: 30, bottom: 55 },
      xAxis: { type: "category", data: dates, ...AXIS }, yAxis: { type: "value", scale: true, ...AXIS },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 10, borderColor: "#232c38" }],
      series: [
        { name: "策略净值", type: "line", data: nav.map(r => +(r.nav / 1e6).toFixed(4)), showSymbol: false, lineStyle: { width: 1.8, color: "#4f8cff" } },
        { name: "基准(沪深300)", type: "line", data: nav.map(r => +(r.bench_nav / 1e6).toFixed(4)), showSymbol: false, lineStyle: { width: 1.2, color: "#8b98a9" } }] });
    // drawdown
    let peak = -1e18; const dd = nav.map(r => { peak = Math.max(peak, r.nav); return +(r.nav / peak - 1).toFixed(5); });
    chart("ch-dd", { tooltip: { ...TOOLTIP, trigger: "axis" }, grid: { left: 55, right: 20, top: 12, bottom: 28 },
      xAxis: { type: "category", data: dates, ...AXIS }, yAxis: { type: "value", ...AXIS },
      series: [{ type: "line", data: dd, showSymbol: false, areaStyle: { color: "rgba(229,83,75,.25)" }, lineStyle: { color: "#e5534b", width: 1 } }] });
    // rolling sharpe (63d)
    const rs = rollingSeries(nav.map(r => r.ret), 63);
    chart("ch-rs", { tooltip: { ...TOOLTIP, trigger: "axis" }, grid: { left: 55, right: 20, top: 12, bottom: 28 },
      xAxis: { type: "category", data: dates, ...AXIS }, yAxis: { type: "value", ...AXIS },
      series: [{ type: "line", data: rs, showSymbol: false, lineStyle: { color: "#3fb68b", width: 1.4 } }] });
  };
  const metrics = extractMetrics(d.backtest, nav);
  const gates = latestGates(sid);
  $("bt-detail").innerHTML = `
    <div class="sec-title" style="display:flex;justify-content:space-between;align-items:center">
      <div>回测详情 · ${esc(sid)} <span class="muted mono">${esc(main.bt_id)}</span>
        ${d.validation ? badge(d.validation.verdict) : ""} ${main.uses_oos ? badge("KNOWN") : ""}</div>
      <button id="btn-eval-${esc(sid)}" class="btn btn-primary btn-sm" onclick="triggerEvalStrategy('${esc(sid)}')">
        ▶ 立即评估与回测该策略
      </button>
    </div>
    <div class="grid c2">
      <div class="card"><h3>净值走势 vs 基准沪深300（百万元）</h3><div id="ch-nav" class="chart tall"></div></div>
      <div class="card"><h3>关键风控与收益指标</h3><div class="kv">
        ${metrics.map(([k, v, c]) => `<div class="k">${esc(k)}</div><div class="v ${c || ""}">${esc(v)}</div>`).join("")}
      </div>
      <h3 style="margin-top:14px">准入评级闸门</h3>${gates}</div>
    </div>
    <div class="grid c2" style="margin-top:14px">
      <div class="card"><h3>动态回撤走势</h3><div id="ch-dd" class="chart short"></div></div>
      <div class="card"><h3>滚动夏普比率（63个交易日）</h3><div id="ch-rs" class="chart short"></div></div>
    </div>
    <div class="card" style="margin-top:14px"><h3>模拟成交明细记录（最近 400 笔）</h3><div class="scroll">
      <table class="tbl"><tr><th>成交日期</th><th>标的代码</th><th>买卖方向</th><th class="num">成交数量</th>
        <th class="num">成交价格</th><th class="num">交易规费</th></tr>
      ${d.fills.slice(-200).map(f => `<tr><td class="mono">${esc(f.trade_date)}</td><td class="mono">${esc(f.ts_code)}</td>
        <td>${badge(f.side === "BUY" ? "buy" : "sell")}</td><td class="num">${fmt(f.shares, 0)}</td>
        <td class="num">${fmt(f.price, 2)}</td><td class="num">${fmt(f.fee)}</td></tr>`).join("") || '<tr><td colspan="6" class="empty">无成交</td></tr>'}
      </table></div></div>`;
  mkChart();
}

const GATE_NAMES = {
  "sharpe_min": "最低夏普比率门槛 (>0.4)",
  "mdd_max": "最大回撤控制红线 (<30%)",
  "calmar_min": "卡玛比率门槛 (>0.5)",
  "walk_forward_pass": "样本外交叉验证一致性",
};

function latestGates(sid) {
  const s = (state._strats || []).find(x => x.strategy_id === sid);
  const gates = s && s.latest_eval && s.latest_eval.metrics && s.latest_eval.metrics._gates;
  if (!gates) return '<div class="muted">暂无闸门记录</div>';
  return Object.entries(gates).map(([k, g]) =>
    `<div class="gate"><span>${esc(GATE_NAMES[k] || k)}</span><span>${g.value != null ? esc(JSON.stringify(g.value)) : "–"}
     <b class="${g.pass ? "g-pass" : "g-fail"}">${g.pass ? "✓ 合格" : "✗ 未达标"}</b></span></div>`).join("");
}

function extractMetrics(bt, nav) {
  const n = nav.length;
  if (!n) return [];
  const first = nav[0].nav, last = nav[n - 1].nav;
  const rets = nav.map(r => r.ret);
  const mu = rets.reduce((a, b) => a + b, 0) / n;
  const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mu) ** 2, 0) / Math.max(1, n - 1));
  const sharpe = sd > 0 ? mu / sd * Math.sqrt(252) : 0;
  let peak = -1e18, mdd = 0;
  nav.forEach(r => { peak = Math.max(peak, r.nav); mdd = Math.min(mdd, r.nav / peak - 1); });
  const years = n / 252;
  const cagr = Math.pow(last / first, 1 / Math.max(years, 1e-9)) - 1;
  return [["回测区间", `${bt.start} ~ ${bt.end}`], ["累计总收益", pct(last / first - 1), cls(last / first - 1)],
          ["年化复合收益 (CAGR)", pct(cagr), cls(cagr)], ["夏普比率 (Sharpe)", fmt(sharpe), cls(sharpe)],
          ["最大回撤 (MDD)", pct(mdd), cls(mdd)], ["回测总天数", String(n) + " 个交易日"],
          ["回测引擎", esc(bt.engine)], ["数据作用域", esc(bt.scope)]];
}

function rollingSeries(rets, w) {
  const out = [];
  for (let i = 0; i < rets.length; i++) {
    if (i < w) { out.push(null); continue; }
    let mu = 0; for (let j = i - w; j < i; j++) mu += rets[j] || 0;
    mu /= w;
    let v = 0; for (let j = i - w; j < i; j++) { const d = (rets[j] || 0) - mu; v += d * d; }
    const sd = Math.sqrt(v / Math.max(1, w - 1));
    out.push(sd > 0 ? +(mu / sd * Math.sqrt(252)).toFixed(3) : null);
  }
  return out;
}

async function selectFactor(fid) {
  const d = await api("/api/research/factors/" + fid + "/detail");
  const box = $("factor-detail");
  if (!d.stats) {
    box.innerHTML = `<div class="card" style="display:flex;justify-content:space-between;align-items:center;padding:18px">
      <span class="muted">${esc(fid)} 尚未分析（等待研究队列）</span>
      <button id="btn-eval-fac-${esc(fid)}" class="btn btn-primary btn-sm" onclick="triggerEvalFactor('${esc(fid)}')">
        ⚡ 立即测量该因子预测力
      </button>
    </div>`;
    return;
  }
  const q = db_q(d.stats.stats_json);
  box.innerHTML = `
    <div class="sec-title" style="display:flex;justify-content:space-between;align-items:center">
      <div>因子详情 · ${esc(fid)}</div>
      <button id="btn-eval-fac-${esc(fid)}" class="btn btn-primary btn-sm" onclick="triggerEvalFactor('${esc(fid)}')">
        ⚡ 重新测算因子预测力 (RankIC)
      </button>
    </div>
    <div class="grid c2">
      <div class="card"><h3>日度 RankIC 动态走势（次日超额预测力）</h3><div id="ch-ic" class="chart"></div></div>
      <div class="card"><h3>21日分层收益表现（5分位数单调性检验）</h3><div id="ch-q" class="chart"></div>
        <div class="note">样本窗口 ${esc(d.stats.window_start)} ~ ${esc(d.stats.window_end)} ·
          t检验统计量 ${fmt(d.stats.ic_tstat, 1)} · 多空年化超额收益 ${pct(d.stats.long_short_ann)}</div></div>
    </div>`;
  chart("ch-ic", { tooltip: { ...TOOLTIP, trigger: "axis" }, grid: { left: 50, right: 16, top: 14, bottom: 28 },
    xAxis: { type: "category", data: d.ic_series.map(r => r.date), ...AXIS },
    yAxis: { type: "value", ...AXIS },
    series: [{ type: "bar", data: d.ic_series.map(r => r.ic == null ? null : +r.ic.toFixed(3)),
               itemStyle: { color: p => p.value >= 0 ? "#3fb68b" : "#e5534b" } }] });
  if (q && q.quantiles) {
    const names = ["第1组(低)", "第2组", "第3组", "第4组", "第5组(高)"];
    chart("ch-q", { tooltip: { ...TOOLTIP }, grid: { left: 60, right: 16, top: 14, bottom: 28 },
      xAxis: { type: "category", data: names, ...AXIS }, yAxis: { type: "value", ...AXIS, axisLabel: { formatter: v => (v * 100).toFixed(1) + "%", color: "#8b98a9" } },
      series: [{ type: "bar", data: q.quantiles.map(v => +(v * 252).toFixed(4)),
                 itemStyle: { color: p => p.value >= 0 ? "#4f8cff" : "#e5534b" } }] });
  }
}
function db_q(s) { try { return JSON.parse(s || "null"); } catch (e) { return null; } }

/* ---------------- Research Queue ---------------- */
async function pageQueue() {
  const d = await api("/api/research/queue");
  $("page").innerHTML = `
    <div class="action-banner">
      <div>
        <div class="banner-text">≡ 研究任务队列控制台</div>
        <div class="banner-sub">自动检索未分析因子与未完成评估的策略，一键在后台批量完成计算与指标入库</div>
      </div>
      <button id="btn-run-queue" class="btn btn-primary" onclick="triggerRunQueue()">
        ▶ 一键执行队列全部测量任务
      </button>
    </div>
    <div class="sec-title">研究队列任务清单（由因子库与策略库自动排期）</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>任务ID</th><th>任务类型</th><th>测算标的</th><th>优先级</th><th>执行状态</th><th>执行结果说明</th><th>更新时间</th></tr>
      ${d.map(i => `<tr><td class="mono">${esc(i.item_id)}</td><td>${esc(i.kind === 'factor' ? '因子测算' : '策略评估')}</td>
        <td class="mono">${esc(i.target)}</td><td class="num">${esc(i.priority)}</td>
        <td>${badge(i.status)}</td>
        <td class="muted" style="white-space:normal;max-width:420px">${esc((i.result_json || "").slice(0, 90))}</td>
        <td class="muted mono">${esc((i.updated_at || "").slice(0, 16))}</td></tr>`).join("")}
    </table></div></div>`;
}

/* ---------------- Market Explorer ---------------- */
async function pageMarket() {
  const benches = await api("/api/market/benchmarks");
  $("page").innerHTML = `
    <div class="searchbar"><input id="mk-search" placeholder="输入股票/ETF代码或名称，如 600519 / 茅台 / 510300">
      <button class="btn" onclick="mkSearch()">搜索标的</button></div>
    <div id="mk-results"></div>
    <div class="grid c2">
      <div class="card"><h3>K线系统与均线走势（最近 250 个交易日）</h3><div id="ch-k" class="chart tall"></div></div>
      <div class="card"><h3>标的基本资料</h3><div id="mk-info" class="empty">请在上方搜索标的或点击下方基准指数</div>
        <h3 style="margin-top:14px">市场主要基准指数</h3>
        <table class="tbl">${benches.map(b => `<tr class="clickable" onclick="loadSecurity('${esc(b.ts_code)}')">
          <td class="mono">${esc(b.ts_code)}</td><td class="num">${fmt(b.close)}</td>
          <td class="muted mono">${esc(b.trade_date)}</td></tr>`).join("")}</table></div>
    </div>`;
  $("mk-search").addEventListener("keydown", e => { if (e.key === "Enter") mkSearch(); });
  if (benches.length) loadSecurity(benches[1] ? benches[1].ts_code : benches[0].ts_code);
}

async function mkSearch() {
  const q = $("mk-search").value.trim();
  if (!q) return;
  const res = await api("/api/market/search?q=" + encodeURIComponent(q));
  $("mk-results").innerHTML = `<div class="card"><table class="tbl">
    <tr><th>标的代码</th><th>证券简称</th><th>所属板块</th><th>证券类别</th><th>上市日期</th></tr>
    ${res.map(r => `<tr class="clickable" onclick="loadSecurity('${esc(r.ts_code)}')">
      <td class="mono">${esc(r.ts_code)}</td><td>${esc(r.name)}</td><td>${esc(r.board)}</td>
      <td>${esc(r.sec_type)}</td><td class="mono">${esc(r.list_date || "")}</td></tr>`).join("") ||
      '<tr><td colspan="5" class="empty">未找到匹配的证券</td></tr>'}</table></div>`;
}
window.mkSearch = mkSearch;

async function loadSecurity(code) {
  state.marketCode = code;
  const [d, pred] = await Promise.all([
    api("/api/market/security?code=" + encodeURIComponent(code) + "&days=250"),
    api("/api/market/predict?code=" + encodeURIComponent(code)).catch(() => null)
  ]);
  const k = d.kline.filter(r => r.close != null);
  const dates = k.map(r => r.trade_date);
  const ma = (n) => k.map((_, i) => i < n - 1 ? null : +(k.slice(i - n + 1, i + 1).reduce((a, x) => a + x.close, 0) / n).toFixed(2));
  const i = d.info;

  let predHtml = "";
  if (pred && pred.ok) {
    const scoreCls = pred.score >= 60 ? 'pos' : pred.score <= 40 ? 'neg' : 'amber';
    predHtml = `
    <div class="predict-panel">
      <div class="predict-header">
        <div>
          <span style="font-weight:600;font-size:13.5px">🔮 智能量化预测与诊断</span>
          <span class="muted mono" style="font-size:11px;margin-left:6px">${esc(pred.trade_date || "")}</span>
        </div>
        <span class="badge ${esc(pred.signal)}">${esc(pred.signal_name)}</span>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between">
        <div class="predict-score-box">
          <span class="predict-score-val ${scoreCls}">${pred.score}</span>
          <span class="predict-score-max">/ 100 量化分</span>
        </div>
        <button class="btn btn-sm" onclick="loadSecurity('${esc(code)}')">🔄 重新诊断</button>
      </div>
      <div class="predict-grid">
        <div class="predict-metric"><div class="lbl">关键支撑位</div><div class="val pos">¥${fmt(pred.support)}</div></div>
        <div class="predict-metric"><div class="lbl">关键压力位</div><div class="val neg">¥${fmt(pred.resistance)}</div></div>
        <div class="predict-metric"><div class="lbl">20日乖离率</div><div class="val">${fmt(pred.bias20)}%</div></div>
      </div>
      <ul class="predict-reasons">
        ${(pred.reasons || []).map(r => `<li>${esc(r)}</li>`).join("")}
      </ul>
    </div>`;
  }

  $("mk-info").innerHTML = `<div class="kv">
    <div class="k">代码</div><div class="v">${esc(i.ts_code)}</div>
    <div class="k">名称</div><div class="v">${esc(i.name)}</div>
    <div class="k">板块</div><div class="v">${esc(i.board)}</div>
    <div class="k">类型</div><div class="v">${esc(i.sec_type)}</div>
    <div class="k">上市日</div><div class="v">${esc(i.list_date || "–")}</div>
    <div class="k">退市日</div><div class="v">${esc(i.delist_date || "–")}</div></div>
    ${predHtml}`;
  chart("ch-k", {
    tooltip: { ...TOOLTIP, trigger: "axis", axisPointer: { type: "cross" } },
    legend: { data: ["MA5", "MA20", "MA60"], textStyle: { color: "#8b98a9" }, top: 0 },
    grid: [{ left: 60, right: 16, top: 26, height: "52%" }, { left: 60, right: 16, top: "72%", height: "16%" }],
    xAxis: [{ type: "category", data: dates, ...AXIS, gridIndex: 0 },
            { type: "category", data: dates, ...AXIS, gridIndex: 1, axisLabel: { show: false } }],
    yAxis: [{ type: "value", scale: true, ...AXIS }, { type: "value", ...AXIS, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    series: [
      { type: "candlestick", data: k.map(r => [r.open, r.close, r.low, r.high]),
        itemStyle: { color: "#e5534b", color0: "#3fb68b", borderColor: "#e5534b", borderColor0: "#3fb68b" } },
      { name: "MA5", type: "line", data: ma(5), showSymbol: false, lineStyle: { width: 1, color: "#d9a53f" } },
      { name: "MA20", type: "line", data: ma(20), showSymbol: false, lineStyle: { width: 1, color: "#4f8cff" } },
      { name: "MA60", type: "line", data: ma(60), showSymbol: false, lineStyle: { width: 1, color: "#9d7cd8" } },
      { type: "bar", data: k.map(r => r.vol), xAxisIndex: 1, yAxisIndex: 1, itemStyle: { color: "rgba(79,140,255,.4)" } }] });
}
window.loadSecurity = loadSecurity;

/* ---------------- PAPER ---------------- */
async function pagePaper() {
  const accs = await api("/api/paper/accounts");
  if (!accs.length) {
    $("page").innerHTML = `<div class="card"><div class="empty">尚无模拟交易账户。
      只有通过严格风控闸门（稳健性 > 样本外 > 风险控制 > 收益率）的策略才会自动进入模拟盘。</div></div>`;
    return;
  }
  $("page").innerHTML = `
    <div class="action-banner">
      <div>
        <div class="banner-text">⚡ 模拟盘自动化预测与交易控制台</div>
        <div class="banner-sub">一键运行全市场量化模型预测与模拟盘盯市，自动计算明日推荐买入/卖出标的与仓位</div>
      </div>
      <div style="display:flex;gap:10px">
        <button class="btn btn-success" onclick="openAddPositionModal()">➕ 手动录入持仓</button>
        <button id="btn-run-paper" class="btn btn-primary" onclick="triggerPaperStep()">
          ⚡ 运行预测 · 生成最新交易信号
        </button>
      </div>
    </div>
    <div class="grid c3">${accs.map(a => `
      <div class="card clickable" style="cursor:pointer" onclick="selPaper('${esc(a.strategy_id)}')">
        <h3>${esc(a.strategy_id)} ${badge(a.status || "ACTIVE")}</h3>
        <div class="kpi-value ${cls(a.return_pct)}">¥${fmt((a.nav || 0) / 1e4, 1)} <span style="font-size:13px">万元</span></div>
        <div class="kpi-sub">累计收益率 ${pct((a.return_pct || 0) / 100)} · 持仓 ${a.n_positions} 只标的 ·
          最近业务日 ${esc((a.last_nav || {}).trade_date || "–")}</div></div>`).join("")}</div>
    <div id="paper-detail"></div>`;
  selPaper(accs[0].strategy_id);
}
window.selPaper = selPaper;

async function selPaper(sid) {
  const d = await api("/api/paper/" + sid);
  const nav = d.nav;
  const lastSig = d.signals[0];
  const labels = lastSig ? JSON.parse(lastSig.payload_json) : [];
  $("paper-detail").innerHTML = `
    <div class="sec-title">模拟盘策略账户资产 · ${esc(sid)}</div>
    <div class="grid c2">
      <div class="card"><h3>模拟净值曲线走势</h3><div id="ch-pnav" class="chart"></div></div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h3 style="margin:0">当前持仓组合明细</h3>
          <button class="btn btn-success btn-sm" onclick="openAddPositionModal('${esc(sid)}')">➕ 手动录入持仓股票</button>
        </div>
        <div class="scroll"><table class="tbl">
          <tr><th>标的代码</th><th>标的简称</th><th class="num">持仓数量</th><th class="num">可用(T+1)</th><th class="num">成本价</th><th class="num">现价</th><th class="num">持仓总市值</th><th>操作</th></tr>
          ${d.positions.map(p => `<tr><td class="mono">${esc(p.ts_code)}</td>
            <td style="font-weight:600">${esc(p.name || p.ts_code)}</td>
            <td class="num">${fmt(p.shares, 0)}</td><td class="num">${fmt(p.available_shares, 0)}</td>
            <td class="num">¥${fmt(p.avg_cost, 2)}</td><td class="num">¥${fmt(p.last_close, 2)}</td>
            <td class="num">¥${fmt(p.shares * (p.last_close || p.last_price || 0), 0)}</td>
            <td><button class="btn ghost btn-sm" style="color:var(--red);border-color:transparent;padding:2px 5px" onclick="triggerRemovePosition('${esc(p.ts_code)}','${esc(sid)}')">清仓</button></td></tr>`).join("") ||
            '<tr><td colspan="8" class="empty">当前账户为空仓状态，可点击右上角添加持仓或运行策略预测</td></tr>'}</table></div></div>
    </div>
    <div class="grid c2" style="margin-top:14px">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h3 style="margin:0">明日调仓建议与预测信号</h3>
          <button class="btn btn-primary btn-sm" onclick="triggerPaperStep()">⚡ 重新测算信号</button>
        </div>
        <div class="muted mono" style="margin-bottom:6px">预测信号生成日期: ${esc(lastSig ? lastSig.signal_date : "–")}</div>
        <table class="tbl">
          <tr><th>标的代码</th><th>标的简称</th><th>操作建议</th><th class="num">目标仓位权重</th></tr>
          ${labels.slice(0, 40).map(l => `<tr>
            <td class="mono">${esc(l.ts_code || "–")}</td>
            <td>${esc(l.name || "–")}</td>
            <td>${badge((l.action || "").replace(/\s/g, "_"))}</td>
            <td class="num">${pct(l.target_weight, 1)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">暂无预测调仓信号</td></tr>'}</table></div>
      <div class="card"><h3>近期模拟撮合成交明细</h3><div class="scroll"><table class="tbl">
        <tr><th>成交日期</th><th>标的代码</th><th>标的简称</th><th>买卖方向</th><th class="num">成交数量</th><th class="num">成交价格</th><th class="num">交易税费</th></tr>
        ${d.fills.slice(0, 30).map(f => `<tr><td class="mono">${esc(f.trade_date)}</td>
          <td class="mono">${esc(f.ts_code)}</td><td>${esc(f.name || "")}</td><td>${badge(f.side === "BUY" ? "buy" : "sell")}</td>
          <td class="num">${fmt(f.shares, 0)}</td><td class="num">¥${fmt(f.price, 2)}</td>
          <td class="num">¥${fmt(f.fee)}</td></tr>`).join("") || '<tr><td colspan="7" class="empty">暂无成交记录</td></tr>'}
        </table></div>
        <h3 style="margin-top:12px">模拟盘系统运行日志</h3>
        ${d.log.slice(0, 10).map(l => `<div class="gate"><span class="muted mono">${esc(l.ts.slice(5, 16))}</span>
          <span class="mono" style="font-size:11.5px">${esc(l.message.slice(0, 70))}</span></div>`).join("")}</div>
    </div>`;
  chart("ch-pnav", { tooltip: { ...TOOLTIP, trigger: "axis" }, grid: { left: 60, right: 20, top: 14, bottom: 40 },
    xAxis: { type: "category", data: nav.map(r => r.trade_date), ...AXIS },
    yAxis: { type: "value", scale: true, ...AXIS },
    dataZoom: [{ type: "inside" }],
    series: [{ type: "line", data: nav.map(r => +(r.nav / 1e6).toFixed(4)), showSymbol: false,
               areaStyle: { color: "rgba(79,140,255,.12)" }, lineStyle: { color: "#4f8cff", width: 1.8 } }] });
}

/* ---------------- Events ---------------- */
async function pageEvents() {
  const d = await api("/api/events/overview");
  $("page").innerHTML = `
    <div class="sec-title">事件驱动超额收益统计（事件研究窗口内 CAR 累计异常超额表现）</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>重大事件类型</th><th class="num">历史事件数</th><th class="num">次日超额 (CAR+1)</th><th class="num">5日超额 (CAR+5)</th>
          <th class="num">10日超额 (CAR+10)</th><th class="num">20日超额 (CAR+20)</th><th class="num">上涨胜率</th></tr>
      ${d.stats.map(s => `<tr><td class="mono">${esc(s.kind)}</td><td class="num">${s.n_events}</td>
        <td class="num ${cls(s.car_1)}">${pct(s.car_1)}</td><td class="num ${cls(s.car_5)}">${pct(s.car_5)}</td>
        <td class="num ${cls(s.car_10)}">${pct(s.car_10)}</td><td class="num ${cls(s.car_20)}">${pct(s.car_20)}</td>
        <td class="num">${pct(s.hit_rate)}</td></tr>`).join("") || '<tr><td colspan="7" class="empty">暂无事件统计数据</td></tr>'}
      </table></div>
      <div class="note">规则化分类引擎（业绩预增、扭亏、股份回购、股东减持、立案退市等），纯量化统计，不依赖不可控的LLM生成买卖决策。</div></div>
    <div class="grid c2" style="margin-top:14px">
      <div class="card"><h3>重大事件发生频次分布</h3><div id="ch-evk" class="chart short"></div></div>
      <div class="card"><h3>最近市场重大公告动态</h3><div class="scroll"><table class="tbl">
        <tr><th>披露时间</th><th>标的代码</th><th>事件分类</th><th class="num">影响评分</th><th>公告核心标题</th></tr>
        ${d.recent.slice(0, 30).map(e => `<tr><td class="mono">${esc((e.event_ts || "").slice(0, 16))}</td>
          <td class="mono">${esc(e.ts_code)}</td><td class="mono">${esc(e.kind)}</td>
          <td class="num ${cls(e.score)}">${fmt(e.score, 1)}</td>
          <td class="muted" style="white-space:normal;max-width:280px">${esc((e.title || "").slice(0, 60))}</td></tr>`).join("")}
        </table></div></div>
    </div>`;
  chart("ch-evk", { tooltip: { ...TOOLTIP }, grid: { left: 60, right: 16, top: 14, bottom: 28 },
    xAxis: { type: "category", data: d.by_kind.map(k => k.kind), ...AXIS, axisLabel: { rotate: 30, fontSize: 9.5, color: "#8b98a9" } },
    yAxis: { type: "value", ...AXIS },
    series: [{ type: "bar", data: d.by_kind.map(k => k.n), itemStyle: { color: "#4f8cff" } }] });
}

/* ---------------- Data Health ---------------- */
async function pageData() {
  const d = await api("/api/data/health");
  $("page").innerHTML = `
    <div class="sec-title">核心底层数据集健康度</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>数据集合</th><th>健康状态</th><th class="num">记录总数</th><th>起始日期</th><th>最新覆盖日期</th>
          <th class="num">滞后天数</th><th class="num">质量评分</th></tr>
      ${d.health.map(h => `<tr><td class="mono">${esc(h.dataset)}</td><td>${badge(h.status)}</td>
        <td class="num">${(h.rows_total / 1e4).toFixed(1)}万行</td><td class="mono">${esc(h.min_date || "–")}</td>
        <td class="mono">${esc(h.max_date || "–")}</td><td class="num">${h.stale_days}</td>
        <td class="num">${fmt(h.quality_score, 0)}分</td></tr>`).join("")}</table></div></div>
    <div class="sec-title">策略专用数据就绪度检查</div>
    <div class="grid c3">${d.readiness.map(r => `
      <div class="card"><h3>${esc(r.strategy_id)}</h3>${badge(r.ready ? "OK" : "WARN")}
        <div style="margin-top:8px">${r.checks.map(c => `<div class="gate">
          <span class="mono muted">${esc(c.dataset)}${c.optional ? " (选填)" : ""}</span>
          <span>${badge(c.ok ? "OK" : (c.status || "MISSING"))} <span class="muted">${c.stale_days == null ? "" : c.stale_days + "天"}</span></span>
          </div>`).join("")}</div></div>`).join("")}</div>
    <div class="sec-title">增量同步断点水线 (Checkpoint)</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>数据集代码</th><th>检查点键名</th><th>最新同步断点值</th><th>最近更新时间</th></tr>
      ${d.watermarks.map(w => `<tr><td class="mono">${esc(w.dataset)}</td><td class="mono">${esc(w.key)}</td>
        <td class="mono">${esc(w.last_value)}</td><td class="muted mono">${esc((w.updated_at || "").slice(0, 16))}</td></tr>`).join("")}
      </table></div></div>`;
}

/* ---------------- System ---------------- */
async function pageSystem() {
  const [d, cfg] = await Promise.all([api("/api/system/runs"), api("/api/system/config")]);
  $("page").innerHTML = `
    <div class="grid c3">
      ${kpiCard("平台版本", "v" + cfg.version, "本地根目录: " + esc(cfg.root))}
      ${kpiCard("环境安全级别", "仅模拟盘模式", "禁止实盘外发 · 无券商访问 · 无资金账户权限")}
      ${kpiCard("数据源配置状态", Object.entries(cfg.secrets).map(([k, v]) => k + (v.configured ? " ✓已配置" : " ✗未配置")).join(" · "), "数据凭据仅存于本地隔离配置")}
    </div>
    <div class="sec-title">系统调度执行总账（支持幂等防重与崩溃恢复）</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>运行ID</th><th>调度任务名称</th><th>业务日期</th><th>执行状态</th><th class="num">重试次数</th><th>启动时间</th><th>异常错误信息</th></tr>
      ${d.runs.map(r => `<tr><td class="mono">${esc(r.run_id.slice(0, 8))}</td><td class="mono">${esc(r.job)}</td>
        <td class="mono">${esc(r.business_date)}</td><td>${badge(r.status)}</td>
        <td class="num">${r.attempts}</td><td class="muted mono">${esc((r.started_at || "").slice(0, 16))}</td>
        <td class="muted" style="white-space:normal;max-width:280px">${esc((r.error || "").slice(0, 80))}</td></tr>`).join("")}
      </table></div></div>
    <div class="sec-title">样本外数据 (OOS) 密封防作弊注册表</div>
    <div class="card"><div class="scroll"><table class="tbl">
      <tr><th>作用域</th><th>密封窗口</th><th>当前状态</th><th>开启/消费时间</th><th>调用任务</th></tr>
      ${d.oos_registry.map(o => `<tr><td class="mono">${esc(o.scope)}</td>
        <td class="mono">${esc(o.start)} ~ ${esc(o.end)}</td><td>${badge(o.status)}</td>
        <td class="muted mono">${esc((o.consumed_at || "").slice(0, 16))}</td>
        <td class="mono">${esc(o.consumed_by || "–")}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">无记录</td></tr>'}
      </table></div></div>
    <div class="sec-title">系统运行告警与日志</div>
    <div class="card">${d.alerts.slice(0, 20).map(a => `<div class="gate">
      <span>${badge(a.level)} <span class="mono">${esc(a.source)}</span></span>
      <span class="muted mono" style="white-space:normal;max-width:520px">${esc((a.message || "").slice(0, 110))}</span></div>`).join("") ||
      '<div class="empty">无告警</div>'}</div>`;
}

/* ---------------- interactive actions ---------------- */
async function triggerPaperStep() {
  const btns = [ $("btn-run-paper"), $("btn-top-paper") ].filter(Boolean);
  const origTexts = btns.map(b => b.innerHTML);
  btns.forEach(b => {
    b.innerHTML = `<span class="spin">◐</span> 正在测算预测信号...`;
    b.classList.add("btn-loading");
  });
  try {
    const res = await postApi("/api/actions/paper_step");
    toast(`模拟盘执行成功！交易日 ${res.result ? res.result.trade_date : ""}，已生成最新候选预测信号`, "success");
    if (state.page === "paper") await pagePaper();
    else if (state.page === "overview") await pageOverview();
  } catch (e) {
    toast("运行预测失败: " + e.message, "error");
  } finally {
    btns.forEach((b, i) => {
      b.innerHTML = origTexts[i];
      b.classList.remove("btn-loading");
    });
  }
}
window.triggerPaperStep = triggerPaperStep;

async function triggerEvalStrategy(sid) {
  const btn = $(`btn-eval-${sid}`);
  if (btn) {
    btn.innerHTML = `<span class="spin">◐</span> 正在评估计算...`;
    btn.classList.add("btn-loading");
  }
  try {
    const res = await postApi(`/api/actions/evaluate_strategy?strategy_id=${encodeURIComponent(sid)}`);
    toast(`策略 ${sid} 评估完成！评级: ${res.verdict || "DONE"}`, "success");
    await pageResearch();
    await selectBacktest(sid);
  } catch (e) {
    toast("评估策略失败: " + e.message, "error");
  } finally {
    const b = $(`btn-eval-${sid}`);
    if (b) {
      b.innerHTML = `▶ 立即评估与回测该策略`;
      b.classList.remove("btn-loading");
    }
  }
}
window.triggerEvalStrategy = triggerEvalStrategy;

async function triggerEvalFactor(fid) {
  const btn = $(`btn-eval-fac-${fid}`);
  if (btn) {
    btn.innerHTML = `<span class="spin">◐</span> 正在测算...`;
    btn.classList.add("btn-loading");
  }
  try {
    const res = await postApi(`/api/actions/evaluate_factor?factor_id=${encodeURIComponent(fid)}`);
    toast(`因子 ${fid} 预测力测算完成！`, "success");
    await pageResearch();
    await selectFactor(fid);
  } catch (e) {
    toast("测算因子失败: " + e.message, "error");
  } finally {
    const b = $(`btn-eval-fac-${fid}`);
    if (b) {
      b.innerHTML = `⚡ 重新测算因子预测力 (RankIC)`;
      b.classList.remove("btn-loading");
    }
  }
}
window.triggerEvalFactor = triggerEvalFactor;

async function triggerRunQueue() {
  const btn = $("btn-run-queue");
  if (!btn) return;
  const orig = btn.innerHTML;
  btn.innerHTML = `<span class="spin">◐</span> 队列任务执行中...`;
  btn.classList.add("btn-loading");
  try {
    const res = await postApi("/api/actions/run_queue");
    toast(`队列任务执行完成，共处理 ${res.processed} 项任务！`, "success");
    await pageQueue();
  } catch (e) {
    toast("执行队列失败: " + e.message, "error");
  } finally {
    const b = $("btn-run-queue");
    if (b) {
      b.innerHTML = orig;
      b.classList.remove("btn-loading");
    }
  }
}
window.triggerRunQueue = triggerRunQueue;

/* ---------------- boot ---------------- */
(function boot() {
  const navEl = $("nav");
  PAGES.forEach(([id, name, ico]) => {
    const item = el("div", { class: "nav-item", "data-page": id, onclick: `nav('${id}')` },
      `<span class="ico">${ico}</span><span>${name}</span>`);
    navEl.appendChild(item);
  });
  render();
})();
