"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let overview = null;
let view = "ALL";
let filter = "ALL";
let token = "";
let requestActive = false;
let pollActive = false;
let notificationsEnabled = false;
const seenSignals = new Set();
const seenAlerts = new Set();
let soundEnabled = false;
let audioContext = null;
let liveConnection = null;
const kinds = {
  WATCH: "关注",
  ENTRY_CANDIDATE: "入场候选",
  RISK: "风险",
  INVALIDATED: "已失效",
};
const states = {
  ARCHIVED: "历史归档",
  ACTIVE: "有效",
  EXPIRED: "已到期",
  INVALIDATED: "已失效",
  RESOLVED: "已解除",
  PAUSED: "暂停使用",
};
const modes = {
  UNKNOWN: "未知",
  NORMAL: "未触发风险",
  CAUTION: "谨慎",
  RISK_OFF: "风险升高",
  LEVERAGE_OVERHEAT: "杠杆过热",
};
const providerStates = {
  READY: "可用",
  DEGRADED: "部分可用",
  UNAVAILABLE: "不可用",
  DISABLED: "未启用",
  NEEDS_VERIFICATION: "待验证",
};
const format = (n, digits = 2) =>
  n == null
    ? "—"
    : Number(n).toLocaleString("en-US", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
const price = (n) =>
  n == null ? "—" : "$" + (n >= 1 ? format(n, 2) : Number(n).toPrecision(4));
const pct = (n) => (n == null ? "—" : (n > 0 ? "+" : "") + format(n, 2) + "%");
const time = (date) =>
  date
    ? new Date(date).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
const cls = (n) => (n == null ? "muted" : n >= 0 ? "up" : "down");
function tag(text, type = "") {
  return `<span class="tag ${type}">${escapeHtml(text)}</span>`;
}
function notice(text) {
  $("#notice").hidden = !text;
  $("#notice").textContent = text || "";
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...options.headers,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (response.status === 401) {
    if (!$("#login").open) $("#login").showModal();
    throw new Error("请填写工作台访问口令");
  }
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  return response;
}
function sparkline(values) {
  if (!values || values.length < 2)
    return '<span class="muted">等待历史观察</span>';
  const low = Math.min(...values),
    high = Math.max(...values),
    spread = high - low || high * 0.001 || 1;
  const points = values
    .map(
      (v, i) =>
        `${((i / (values.length - 1)) * 76).toFixed(2)},${(22 - ((v - low) / spread) * 20).toFixed(2)}`,
    )
    .join(" ");
  return `<svg class="sparkline" viewBox="0 0 76 24" role="img" aria-label="已归档价格走势"><polyline points="${points}" fill="none" stroke="${values.at(-1) >= values[0] ? "#7bceb6" : "#f18e8c"}" stroke-width="1.3"/></svg>`;
}
function assetRisk(asset) {
  if (!asset.data_usable) return tag("仅观察 · 数据不足或过期", "warning");
  if (asset.risk?.blockers?.length) return tag("风险阻断", "danger");
  if (asset.risk?.status === "NEEDS_VERIFICATION")
    return tag("风险待核实", "warning");
  if (asset.quality_issues?.length) return tag("覆盖不完整", "warning");
  return tag("数据可用");
}
function renderAssets() {
  if (!overview) return;
  const query = $("#search").value.toLowerCase().trim();
  const items = overview.assets.filter(
    (a) =>
      (filter === "MEME" ? a.module === "MEME" : a.module !== "MEME" && (filter === "ALL" || a.module === filter)) &&
      `${a.symbol} ${a.address || ""} ${a.asset_id}`
        .toLowerCase()
        .includes(query),
  );
  items.sort(
    (a, b) =>
      ({ BTC: 0, ETH: 1, ALT: 2, MEME: 3 })[a.module] -
      { BTC: 0, ETH: 1, ALT: 2, MEME: 3 }[b.module],
  );
  $("#assets").innerHTML = items
    .map((a) => {
      const f = a.features;
      const buy =
        a.module === "MEME"
          ? f.buys_5m != null &&
            f.sells_5m != null &&
            f.buys_5m + f.sells_5m > 0
            ? f.buys_5m / (f.buys_5m + f.sells_5m)
            : null
          : f.spot_taker_buy_ratio;
      return `<tr data-asset="${escapeHtml(a.asset_id)}" class="${a.data_usable ? "" : "asset-stale"}" tabindex="0"><td><div class="asset-name"><span class="coin-avatar ${a.module.toLowerCase()}">${a.module === "BTC" ? "₿" : escapeHtml(a.symbol[0])}</span><div><strong>${escapeHtml(a.symbol)}</strong><small>${escapeHtml(a.chain || a.source)} · ${a.module}</small></div></div></td><td>${price(a.price)}</td><td class="${cls(f.return_5m_pct)}">${pct(f.return_5m_pct)}</td><td>${f.relative_volume != null ? format(f.relative_volume) + "×" : "—"}</td><td>${buy != null ? format(buy * 100, 0) + "%" : "—"}${a.module === "MEME" ? '<small class="muted"> 笔数</small>' : ""}</td><td>${f.funding_rate_pct != null ? format(f.funding_rate_pct, 4) + "%" : "—"}</td><td>${a.module === "MEME" ? "池龄 " + (f.pool_age_hours != null ? format(f.pool_age_hours, 1) + "h" : "未知") : sparkline(a.trend)}</td><td>${assetRisk(a)}<small>报价 ${time(a.market_time)} · 详细 ${time(a.detail_updated_at)} · 24h成交额 ${price(a.quote_volume_24h)}</small><small>${escapeHtml(a.quality_issues.join(" · "))}</small></td></tr>`;
    })
    .join("");
  $("#empty-assets").hidden = items.length > 0;
  $("#asset-footer").textContent =
    `${items.length} 个标的 · ${items.filter((a) => a.data_usable).length} 个数据可用`;
}
function signalScore(s) {
  if (s.kind === "INVALIDATED") return "原候选失效记录 · 不代表新的看空判断";
  if (s.kind === "RISK") return "市场风险 · 以当前条件与状态为准";
  if (s.model_version === "context-observation") return "研究观察 · 尚未校准";
  return `规则证据 ${format(s.evidence_score, 0)}（非概率）`;
}
function renderSignals() {
  const items = overview.signals
    .filter((s) => !["BTC", "ETH", "ALT", "MEME"].includes(view) || s.module === view)
    .slice(0, view === "SIGNALS" ? 100 : 5);
  $("#signals").innerHTML = items
    .map(
      (s) =>
        `<article class="signal-row" data-signal="${escapeHtml(s.id)}" tabindex="0"><div class="signal-top">${tag(kinds[s.kind] || s.kind, s.kind === "RISK" ? "danger" : ["WATCH", "INVALIDATED"].includes(s.kind) ? "warning" : "")}<strong>${escapeHtml(s.symbol)}</strong><time>${time(s.emitted_at)}</time></div><p>${escapeHtml(s.title)}</p><div class="signal-meta"><span>${escapeHtml(states[s.state] || s.state)}</span><span>${escapeHtml(signalScore(s))}</span><span>${(s.evaluation?.primary_horizon_seconds ?? s.horizon_seconds) / 60}m ${s.evaluation ? "主评估期" : "历史观察窗"}</span><span>${escapeHtml(s.rule_id)}</span></div></article>`,
    )
    .join("");
  $("#empty-signals").hidden = items.length > 0;
}
function renderOverview() {
  $("#risk-mode").textContent =
    modes[overview.regime.risk_mode] || overview.regime.risk_mode;
  $("#risk-mode").className =
    "metric-value " +
    (["RISK_OFF", "LEVERAGE_OVERHEAT"].includes(overview.regime.risk_mode)
      ? "down"
      : overview.regime.risk_mode === "NORMAL"
        ? "up"
        : "");
  $("#risk-caption").textContent = overview.regime.btc_snapshot_id
    ? "基于当前可用的 BTC 证据"
    : "等待可用的真实 BTC 数据";
  $("#alt-count").textContent = overview.assets.filter(
    (a) => a.module === "ALT" && a.data_usable,
  ).length;
  const coverage = overview.coverage || {};
  $("#meme-count").textContent = `${coverage.quotes || 0}/${coverage.target || 102}`;
  $("#universe-caption").textContent = `报价覆盖 ${coverage.quotes || 0}/${coverage.target || 102} · 本轮详细数据 ${coverage.details || 0} · 更新 ${time(coverage.updated_at)} · 缺失报价 ${(coverage.missing_quotes || []).join(", ") || "无"}`;
  const unread = (overview.alerts || []).filter((a) => a.unread).length;
  $("#signal-count").textContent = unread;
  $("#alert-badge").textContent = unread;
  $("#btc-structure").textContent =
    { UNKNOWN: "未知", UPTREND: "EMA20 > EMA50", DOWNTREND: "EMA20 ≤ EMA50" }[
      overview.regime.btc_structure
    ] || "未知";
  $("#regime-evidence").innerHTML = overview.regime.evidence
    .map((e) => `<li>${escapeHtml(e)}</li>`)
    .join("");
  $("#providers").innerHTML = overview.providers
    .map(
      (p) =>
        `<div class="provider"><div class="provider-top"><span>${escapeHtml(p.name)}</span>${tag(providerStates[p.state] || p.state, p.state === "READY" ? "" : p.state === "DISABLED" ? "disabled" : p.state === "UNAVAILABLE" ? "danger" : "warning")}</div><p>${escapeHtml(p.message)}</p><small>${escapeHtml(p.coverage)}</small></div>`,
    )
    .join("");
  $("#cycle-status").textContent = overview.collector.busy
    ? "正在采集"
    : overview.collector.enabled ? `每 ${overview.collector.poll_seconds}s 自动采集` : "自动采集未启用";
  const finished = overview.collector.last_result.finished_at;
  $("#last-updated").textContent = finished
    ? "最近采集 " + time(finished)
    : "尚无本次启动的采集记录";
  $("#connection").textContent = "工作台已连接";
  $("#connection").classList.add("online");
  const required = overview.providers.filter((p) =>
    ["Binance Spot"].includes(p.name),
  );
  if (required.some((p) => p.state === "UNAVAILABLE"))
    notice(
      "部分公开数据源当前不可访问。系统会自动重试；市场状态和机会提醒需等待真实数据恢复。详见数据源状态。",
    );
  else if (
    overview.assets.length &&
    overview.assets.every((a) => !a.data_usable)
  )
    notice("归档行情已过期或未通过质量检查，当前暂停机会判断。");
  else if (!requestActive) notice("");
  renderAssets();
  renderSignals();
  renderV4();
}
async function loadOverview() {
  if (pollActive) return;
  pollActive = true;
  try {
    const first = overview === null;
    overview = await (await api("/api/overview")).json();
    if (!first) groupAlerts((overview.alerts || []).filter((a) =>
      a.notification_eligible))
      .filter((g) => g.members.some((a) => !seenAlerts.has(a.id + ":" + (a.notification_id || a.notification_revision || a.first_seen))))
      .forEach((g) => {
        const a = g.primary;
        if (notificationsEnabled) new Notification(`${a.symbol} · ${a.display_level || a.level}`, {body: a.message_zh || g.members.map(m => m.title).join("；"), tag: g.key});
        if (soundEnabled && audioContext) {
          const oscillator = audioContext.createOscillator();
          const gain = audioContext.createGain();
          gain.gain.value = 0.08;
          oscillator.connect(gain); gain.connect(audioContext.destination);
          oscillator.start(); oscillator.stop(audioContext.currentTime + 0.15);
        }
      });
    (overview.alerts || []).forEach((a) => seenAlerts.add(a.id + ":" + (a.notification_id || a.notification_revision || a.first_seen)));
    overview.signals.forEach((s) => seenSignals.add(s.id));
    renderOverview();
    if (!liveConnection) connectLive();
  } catch (error) {
    $("#connection").textContent = "连接中断";
    $("#connection").classList.remove("online");
    notice(error.message);
  } finally {
    pollActive = false;
  }
}
async function showAsset(id) {
  try {
    const a = await (await api(`/api/assets/${encodeURIComponent(id)}`)).json();
    $("#detail-title").textContent = a.symbol + " · 标的依据";
    const labels = {
      return_5m_pct: "5m 变化 %",
      return_15m_pct: "15m 变化 %",
      return_1h_pct: "1h 变化 %",
      relative_volume: "相对成交量",
      volume_acceleration: "成交量加速度",
      spot_taker_buy_ratio: "现货主动买入占比",
      perp_taker_buy_ratio: "合约主动买入占比",
      spot_cvd_window: "现货 15m CVD（报价币）",
      perp_cvd_window: "合约 15m CVD（报价币）",
      funding_rate_pct: "Funding %",
      oi_change_5m_pct: "OI 5m 变化 %",
      basis_pct: "Basis %",
      relative_strength_15m_pct: "相对 BTC 15m 强弱 %",
      relative_strength_eth_15m_pct: "相对 ETH 15m 强弱 %",
      oi_usd: "当前 OI（USD）",
      oi_zscore: "OI Z-score",
      funding_zscore: "Funding Z-score",
      perp_taker_delta_usd: "合约主动买卖差额（USD）",
      long_short_account_ratio: "账户多空比",
      elite_account_ratio: "大户账户多空比",
      elite_position_ratio: "大户持仓多空比",
      cross_venue_premium_pct: "OKX Mark / 币安现货溢价 %",
      perp_spread_bps: "合约价差 bps",
      spot_sample_cvd_usdt: "现货近期成交样本 CVD（USDT）",
      perp_sample_cvd_usdt: "合约近期成交样本 CVD（USDT）",
      liquidity_usd: "池子流动性 USD",
      rsi14: "RSI 14",
      atr14: "ATR 14",
      spread_bps: "价差 bps",
    };
    const r = (overview?.rankings || []).find((row) => row.asset_id === a.asset_id);
    const rankDetail = r ? `<h3>排名与评分依据</h3><p>当前 #${r.rank} · 前次 ${r.previous_rank ?? "—"} · 排名变化 ${format(r.rank_change,0)} · 评分变化 ${format(r.score_delta)} · 覆盖 ${format(r.coverage_pct,0)}%</p><ul>${Object.entries(r.score_components).map(([key,value]) => `<li>${escapeHtml(key)}：${format(value)} · 变化 ${format(r.score_changes[key])}</li>`).join("")}</ul><p>缺少：${escapeHtml(r.missing.join(" · ") || "无")}</p>` : "";
    $("#detail-body").innerHTML =
      `<p>${escapeHtml(a.asset_id)}</p><div class="detail-grid"><div class="detail-item"><span>公开观察价格</span><strong>${price(a.price)}</strong></div><div class="detail-item"><span>数据可用时间 UTC+8</span><strong>${time(a.available_at)}</strong></div></div><h3>特征</h3><div class="detail-grid">${Object.entries(
        labels,
      )
        .map(
          ([key, label]) =>
            `<div class="detail-item"><span>${label}</span><strong>${format(a.features[key], 4)}</strong></div>`,
        )
        .join(
          "",
        )}</div>${rankDetail}<h3>现货 / 合约结构假设</h3><p>${escapeHtml(a.features.spot_perp_structure || "数据不足")} · 去杠杆 ${a.features.deleveraging_signal === null ? "未能判断" : a.features.deleveraging_signal ? "观察到条件" : "未触发"}（非真实清算金额）</p><h3>组件源时间</h3><ul>${Object.entries(a.component_times || {}).map(([key,value]) => `<li>${escapeHtml(key)}：${time(value)}</li>`).join("")}</ul>${a.risk ? `<h3>代币风险检查 · ${escapeHtml(a.risk.status)}</h3><ul>${[...a.risk.blockers, ...a.risk.missing_checks.map((x) => "尚未核实：" + x), ...a.risk.warnings].map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}<h3>数据质量</h3><p>${escapeHtml(a.quality_issues.join(" · ") || "当前检查未发现缺项")}</p><h3>原始数据追溯</h3><div>${a.raw_ids.map((id, i) => `<button class="button secondary raw-button" data-raw="${escapeHtml(id)}">观察 ${i + 1}</button>`).join(" ")}</div><div id="raw-body"></div>`;
    if (!$("#detail").open) $("#detail").showModal();
  } catch (e) {
    notice(e.message);
  }
}
function evaluationDetail(s) {
  if (!s.evaluation) return "<p>历史记录仅声明观察窗，未提前声明持有期和主指标。</p>";
  const p = s.evaluation;
  const names = {LONG_RETURN:"价格表现", RISK_DIRECTION:"下跌方向及风险路径", VOLATILITY:"波动范围及绝对变化"};
  const auxiliary = p.horizons_seconds.filter(h => h !== p.primary_horizon_seconds);
  return `<h3>提前声明的评估</h3><p>${escapeHtml(names[p.metric] || p.metric)} · 主评估期 ${p.primary_horizon_seconds / 60}m · 辅助观察 ${auxiliary.length ? auxiliary.map(h => `${h / 60}m`).join(" / ") : "无"}</p><p>${escapeHtml(p.rationale)}</p><p class="muted">评估到期只记录表现；五分钟负收益不会自动使信号失效。有效期和失效条件独立判断。</p>`;
}
function outcomeDetail(o, s) {
  const prefix = `${o.horizon_seconds / 60}m · ${!s.evaluation ? "历史观察" : o.horizon_seconds === s.evaluation.primary_horizon_seconds ? "主评估" : "辅助观察"}`;
  if (s.evaluation && (o.evaluation_policy_id !== s.evaluation_policy_id || o.evaluation_metric !== s.evaluation.metric || !s.evaluation.horizons_seconds.includes(o.horizon_seconds))) {
    return `${prefix} · 口径未匹配声明，仅保留原始记录：价格变化 ${pct(o.return_pct)}`;
  }
  if (o.evaluation_metric === "RISK_DIRECTION") {
    const flag = value => value == null ? "未记录" : value ? "是" : "否";
    return `${prefix}：终点下跌 ${flag(o.risk_terminal_decline)}，窗口曾下跌 ${flag(o.risk_window_decline)}，最大采样跌幅 ${pct(o.risk_max_decline_pct)}，最大反向上涨 ${pct(o.risk_max_rebound_pct)}；不计算持仓收益`;
  }
  if (o.evaluation_metric === "VOLATILITY") return `${prefix}：采样范围 ${pct(o.observed_range_pct)}，终点绝对变化 ${pct(o.absolute_end_change_pct)}；不判断涨跌方向`;
  return `${prefix}：价格变化 ${pct(o.return_pct)}，最大不利变化 ${pct(o.max_adverse_pct)}，${o.sample_count} 次采样`;
}
async function showSignal(id) {
  try {
    const { signal: s, outcomes } = await (
      await api(`/api/signals/${encodeURIComponent(id)}`)
    ).json();
    $("#detail-title").textContent = s.symbol + " · " + s.title;
    const list = (values) =>
      `<ul>${values.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
    $("#detail-body").innerHTML =
      `<p>${tag(kinds[s.kind])} ${tag(states[s.state] || s.state)} ${tag("规则观察期", "warning")}</p><div class="detail-grid"><div class="detail-item"><span>触发时价格</span><strong>${price(s.reference_price)}</strong></div><div class="detail-item"><span>失效参考位</span><strong>${price(s.invalidation_price)}</strong></div><div class="detail-item"><span>入场参考区间</span><strong>${s.entry_zone ? s.entry_zone.map(price).join(" — ") : "尚未满足入场条件"}</strong></div><div class="detail-item"><span>有效至 UTC+8</span><strong>${time(s.expires_at)}</strong></div></div>${decisionDetail(s.decision)}${evaluationDetail(s)}<h3>触发证据</h3>${list(s.evidence)}<h3>反向证据与限制</h3>${list(s.contradictions.length ? s.contradictions : ["规则尚未完成充分的样本外验证"])}<h3>失效条件</h3>${list(s.invalidation_conditions)}<h3>历史状态</h3>${list(s.events.map((e) => time(e.event_at) + " · " + (states[e.state] || e.state) + " · " + e.reason))}<h3>事后观察</h3>${outcomes.length ? list(outcomes.map((o) => outcomeDetail(o, s))) : "<p>尚未到达观察时间窗，或缺少覆盖该时间窗的真实价格。</p>"}<p class="muted">规则 ${escapeHtml(s.rule_id)} / ${escapeHtml(s.rule_version)} · ${s.evidence_groups.map(escapeHtml).join(" + ")}</p><button class="button secondary" data-asset="${escapeHtml(s.asset_id)}">查看标的原始依据 ↗</button>`;
    if (!$("#detail").open) $("#detail").showModal();
  } catch (e) {
    notice(e.message);
  }
}
function validationTable(rows, metric, legacy = false) {
  if (!rows.length) return "<p>暂无该类型的声明评估样本。</p>";
  const probability = n => n == null ? "—" : `${(n * 100).toFixed(1)}%`;
  const risk = metric === "RISK_DIRECTION", volatility = metric === "VOLATILITY";
  const columns = risk ? ["终点下跌占比 / 95%区间", "窗口曾下跌占比", "最大采样跌幅 / 反向上涨"] : volatility ? ["平均采样范围", "平均终点绝对变化"] : ["平均价格变化", "扣成本 / 双倍成本", "正收益占比 / 95%区间"];
  return `<div class="table-scroll"><table><thead><tr><th>规则 / 版本</th><th>类型 / 模块</th><th>期限 / 用途</th><th>测量 / 覆盖 / 去重</th><th>待到期 / 缺失 / 断档</th>${columns.map(c => `<th>${c}</th>`).join("")}<th>样本状态</th></tr></thead><tbody>${rows.map(row => {
    const role = legacy ? "历史观察" : row.horizon_role === "PRIMARY" ? "主评估" : "辅助观察";
    const stats = risk ? `<td>${probability(row.risk_terminal_decline_rate ?? row.down_move_rate)}<small>${row.risk_terminal_decline_rate_wilson_95?.map(probability).join(" — ") || "—"}</small></td><td>${probability(row.risk_window_decline_rate)}</td><td>${pct(row.risk_mean_max_decline_pct)} / ${pct(row.risk_mean_max_rebound_pct)}</td>` : volatility ? `<td>${pct(row.mean_observed_range_pct)}</td><td>${pct(row.mean_absolute_end_change_pct)}</td>` : `<td>${pct(row.mean_return_pct)}</td><td>${pct(row.mean_paper_net_return_pct)} / ${pct(row.mean_double_cost_return_pct)}</td><td>${probability(row.paper_positive_rate)}<small>${row.paper_positive_rate_wilson_95?.map(probability).join(" — ") || "—"}</small></td>`;
    return `<tr><td>${escapeHtml(row.rule_id)}<small>${escapeHtml(row.rule_version || "旧版本")}</small></td><td>${escapeHtml(kinds[row.signal_kind] || row.signal_kind || "未知")} / ${escapeHtml(row.module || "未知")}</td><td>${row.horizon_seconds / 60}m · ${role}</td><td>${row.measured_count} / ${row.covered_count} / ${row.nonoverlapping_count ?? "—"}</td><td>${row.pending_count ?? 0} / ${row.missing_outcome_count ?? 0} / ${row.gapped_count ?? 0}</td>${stats}<td>${row.evaluation_status === "INSUFFICIENT_SAMPLES" ? "样本不足" : "仅描述统计"}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}
async function loadValidation() {
  try {
    const r = await (await api("/api/validation")).json();
    const current = r.version === "validation-v3" ? r.rules : [];
    const legacy = r.version === "validation-v3" ? r.legacy_rules || [] : r.rules;
    const metric = row => row.evaluation_metric || (row.signal_kind === "RISK" ? "RISK_DIRECTION" : "LONG_RETURN");
    const tables = (rows, historical = false) => [
      ["LONG_RETURN", "机会价格表现"], ["RISK_DIRECTION", "风险方向观察"], ["VOLATILITY", "波动背景观察"]
    ].map(([key, title]) => {
      const selected = rows.filter(row => metric(row) === key);
      return selected.length ? `<h3>${title}</h3>${validationTable(selected, key, historical)}` : "";
    }).join("");
    $("#validation-body").innerHTML =
      `<div class="validation-summary"><div><strong>${r.signal_count}</strong><small>归档提醒</small></div><div><strong>${r.outcome_count}</strong><small>已有测量窗</small></div></div>
      <p>只评估提前声明的期限；主评估与辅助观察分开。五分钟的表现记录不会自动否定信号。风险告警观察下跌和反弹，不计算持仓收益。</p>
      ${current.length ? tables(current) : "<p>尚无带完整评估声明的新信号；旧结果保留在下方。</p>"}
      ${legacy.length ? `<details><summary>历史评估口径 · ${legacy.length} 组结果（与新声明分开）</summary><p>旧记录没有提前声明主指标和完整期限。这里保留已有结果，包括负收益，不作为新规则验收。</p>${tables(legacy, true)}</details>` : ""}
      <ul>${r.limitations.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  } catch (e) {
    notice(e.message);
  }
}
function changeView(next) {
  view = next;
  filter = ["BTC", "ETH", "ALT", "MEME"].includes(view) ? view : "ALL";
  const names = {
    ALL: "市场总览",
    BTC: "BTC 研判",
    ETH: "ETH 核心",
    RANKING: "100 币雷达",
    HEATMAP: "机会热图",
    ALERTS: "Web 预警中心",
    ALT: "山寨币雷达",
    MEME: "Meme 发现池",
    INTELLIGENCE: "研究与扩展",
    SIGNALS: "提醒记录",
    VALIDATION: "历史验证",
  };
  $("#page-title").innerHTML =
    names[view] + '<span class="heading-dot">.</span>';
  $("#breadcrumb-view").textContent = names[view];
  $("#mobile-view").value = view;
  $$(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view),
  );
  $$("[data-filter]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.filter === filter),
  );
  const dedicated = ["RANKING", "HEATMAP", "ALERTS", "INTELLIGENCE"].includes(view);
  $("#intelligence-view").hidden = view !== "INTELLIGENCE";
  $("#ranking-view").hidden = view !== "RANKING";
  $("#heatmap-view").hidden = view !== "HEATMAP";
  $("#alerts-view").hidden = view !== "ALERTS";
  $("#monitor-view").hidden = dedicated || ["SIGNALS", "VALIDATION"].includes(view);
  $("#research-grid").hidden = dedicated || view === "VALIDATION";
  $("#validation-view").hidden = view !== "VALIDATION";
  $(".regime-panel").hidden = view === "SIGNALS";
  $("#research-grid").style.gridTemplateColumns =
    view === "SIGNALS" ? "1fr" : "";
  if (overview) {
    renderAssets();
    renderSignals();
    renderV4();
  }
  if (view === "VALIDATION") loadValidation();
  if (view === "INTELLIGENCE") loadIntelligence();
}
$("#mobile-view").addEventListener("change", (event) =>
  changeView(event.target.value),
);
document.addEventListener("click", async (event) => {
  const b = event.target.closest("button, [data-asset], [data-signal], [data-alert]");
  if (!b) return;
  if (b.dataset.alertAction) {
    try { await api(`/api/alerts/${encodeURIComponent(b.dataset.alertId)}/${b.dataset.alertAction}`, {method: "POST"}); await loadOverview(); $("#detail").close(); }
    catch (e) { notice(e.message); }
  }
  else if (b.dataset.alert) showAlert(b.dataset.alert);
  else if (b.dataset.view) changeView(b.dataset.view);
  else if (b.dataset.filter) {
    filter = b.dataset.filter;
    $$("[data-filter]").forEach((x) => x.classList.toggle("selected", x === b));
    renderAssets();
  } else if (b.dataset.asset) showAsset(b.dataset.asset);
  else if (b.dataset.signal) showSignal(b.dataset.signal);
  else if (b.dataset.raw) {
    try {
      const r = await (
        await api(`/api/raw/${encodeURIComponent(b.dataset.raw)}`)
      ).json();
      $("#raw-body").innerHTML =
        `<h3>原始公开观察 · SHA256 ${escapeHtml(r.payload_hash.slice(0, 12))}</h3><pre>${escapeHtml(JSON.stringify(r, null, 2))}</pre>`;
    } catch (e) {
      notice(e.message);
    }
  }
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    event.target.matches("[data-asset], [data-signal], [data-alert]")
  )
    event.target.click();
});
$("#search").addEventListener("input", renderAssets);
$("#close-detail").addEventListener("click", () => $("#detail").close());
$("#refresh").addEventListener("click", async () => {
  if (requestActive) return;
  requestActive = true;
  $("#refresh").disabled = true;
  $("#refresh").textContent = "采集中…";
  try {
    const result = await (await api("/api/collect", { method: "POST" })).json();
    notice(
      result.status === "COMPLETE"
        ? `采集完成：${result.snapshots} 个快照，${result.signals} 条新提醒。`
        : result.message || "本轮尚未取得可用数据，请查看数据源状态。",
    );
    await loadOverview();
  } catch (e) {
    notice(e.message);
  } finally {
    requestActive = false;
    $("#refresh").disabled = false;
    $("#refresh").textContent = "↻ 立即采集";
  }
});
$("#export").addEventListener("click", async () => {
  try {
    const response = await api("/api/export");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "muse-signals.json";
    link.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    notice(e.message);
  }
});
$("#notify").addEventListener("click", async () => {
  if (notificationsEnabled) {notificationsEnabled = false; $("#notify").textContent = "开启浏览器提醒"; return;}
  if (!("Notification" in window)) {
    notice("此浏览器不支持桌面提醒。");
    return;
  }
  try {
    notificationsEnabled =
      (await Notification.requestPermission()) === "granted";
    $("#notify").textContent = notificationsEnabled
      ? "浏览器提醒已开启"
      : "浏览器提醒未授权";
  } catch (e) {
    notice("浏览器提醒无法开启。");
  }
});
$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  token = $("#token").value;
  $("#token").value = "";
  $("#login").close();
  loadOverview();
});
setInterval(() => {
  $("#clock").textContent =
    new Date().toLocaleTimeString("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour12: false,
    }) + " UTC+8";
}, 1000);
setInterval(() => {
  loadOverview();
  if (view === "VALIDATION") loadValidation();
}, 10000);
loadOverview();

function groupAlerts(alerts) {
  const groups = new Map(), levels = {INFO:0, WATCH:1, SETUP:2, STRONG:3, CRITICAL_RISK:4};
  for (const a of alerts) {
    const cancellation = a.notification_class === "CANCELLATION";
    const separate = (!cancellation && a.level === "CRITICAL_RISK") || a.context?.validation_status === "OBSERVATION_ONLY" || !["ACTIVE", "PAUSED"].includes(a.state);
    const key = cancellation ? `${a.asset_id}:${a.state}:CANCELLATION:${a.cancellation_reason}` : separate ? a.id : `${a.asset_id}:${a.state}:${a.horizon_seconds || 3600}`;
    if (!groups.has(key)) groups.set(key, {key, members:[]});
    groups.get(key).members.push(a);
  }
  return [...groups.values()].map(g => {
    g.members.sort((a,b) => (levels[b.display_level || b.level]-levels[a.display_level || a.level]) || b.last_updated.localeCompare(a.last_updated));
    g.primary = g.members[0];
    g.patterns = [...new Set(g.members.flatMap(a => a.patterns || []))];
    return g;
  });
}
function alertScores(a) {
  if (a.notification_class === "CANCELLATION") return a.parent_delivery_confirmed ? "撤销已发候选 · 不代表新的看空判断" : "观察失效记录 · 原提醒送达未确认";
  if (a.level === "CRITICAL_RISK") return `风险等级：重要风险${a.risk_type === "SIGNAL_INVALIDATED" ? " · 原信号失效" : ""}`;
  if (a.score_schema === "LEGACY_UNSEPARATED") return "旧版评分含义未分离，需查看原证据";
  if (a.context?.validation_status === "OBSERVATION_ONLY") return "研究观察 · 尚未校准";
  return `机会排行 ${format(a.opportunity_score)} · 规则证据 ${format(a.rule_evidence_score)}（非概率）`;
}
function renderV4() {
  if (!overview) return;
  const rows = [...(overview.rankings || [])];
  const key = $("#rank-sort").value;
  rows.sort((a,b) => key === "rank" ? a.rank-b.rank : (b[key] ?? -999)-(a[key] ?? -999));
  const assets = new Map(overview.assets.map((a) => [a.asset_id, a]));
  $("#ranking-rows").innerHTML = rows.map((r) => {
    const f = assets.get(r.asset_id)?.features || {};
    return `<tr data-asset="${escapeHtml(r.asset_id)}" tabindex="0"><td>${r.rank}</td><td>${escapeHtml(r.symbol)}<small>${escapeHtml(r.tier || "")}</small></td><td>${format(r.score)} / ${format(r.score_delta)}</td><td>${format(r.rank_change,0)}</td><td>${price(r.price)}</td><td>${pct(f.oi_change_5m_pct)}</td><td>${pct(f.funding_rate_pct)}</td><td>${pct(f.relative_strength_15m_pct)} / ${pct(f.relative_strength_eth_15m_pct)}</td><td>${format(r.coverage_pct,0)}%${r.data_ready ? "" : " · 仅观察"}</td></tr>`;
  }).join("");
  $("#heatmap").innerHTML = rows.map((r) => `<button class="heat-cell ${r.data_ready ? "" : "missing"}" style="--strength:${r.score / 100}" data-asset="${escapeHtml(r.asset_id)}"><strong>${escapeHtml(r.symbol.replace("USDT", ""))}</strong><span>#${r.rank} · ${format(r.score,0)}</span><small>覆盖 ${format(r.coverage_pct,0)}%</small></button>`).join("");
  const level = $("#alert-level").value, state = $("#alert-state").value;
  const query = $("#alert-search").value.toLowerCase();
  const alerts = (overview.alerts || []).filter((a) => (!level || (a.display_level || a.level) === level) &&
    (!state || (state === "unread" ? a.unread : state === "pinned" ? a.pinned : a.state === state)) &&
    `${a.symbol} ${a.title} ${a.rule_id}`.toLowerCase().includes(query));
  $("#web-alerts").innerHTML = alerts.length ? groupAlerts(alerts).map((g) => {
    const a = g.primary;
    return `<article class="signal-row" data-alert="${escapeHtml(a.id)}" tabindex="0"><div class="signal-top">${tag(a.display_level || a.level, a.notification_class === "MARKET_RISK" ? "danger" : "warning")}<strong>${escapeHtml(a.symbol)}</strong>${g.members.some(m => m.unread) ? tag("未读") : ""}${g.members.some(m => m.pinned) ? tag("已固定") : ""}<time>${time(a.last_updated)}</time></div><p>${[...new Set(g.members.map(m => escapeHtml(m.title)))].join("；")}</p><div class="signal-meta"><span>${escapeHtml(alertScores(a))}</span><span>${escapeHtml(states[a.state] || a.state)}</span><span>首次 ${time(a.first_seen)}</span></div>${g.patterns.length ? `<p>${escapeHtml(g.patterns.join("；"))}</p>` : ""}${g.members.length > 1 ? (a.notification_class === "CANCELLATION" ? '<p>同币同原因的撤销合并，保留每个原候选的关联。</p>' : '<p>同币相关条件合并，分数不相加，不能视作多套独立策略确认。</p>') : ""}<div class="actions">${g.members.map(m => `<button class="button secondary" data-alert="${escapeHtml(m.id)}">${escapeHtml(m.rule_id)}${m.rule_evidence_score != null ? ` · 证据 ${format(m.rule_evidence_score)}` : ""}</button>`).join("")}</div></article>`;
  }).join("") : '<p class="empty">暂无符合条件的真实预警。</p>';
}
function decisionDetail(d, currentContext) {
  if (!d?.version) return '<h3>决策链</h3><p>旧记录未保存完整决策解释，无法确认全部规则的使用情况。</p>';
  const list = values => `<ul>${values.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  const status = value => ({PASS:"通过",BLOCKED:"限制",UNKNOWN:"未知",DISABLED:"未启用",NO_KNOWN_BLOCKER:"未发现已知阻断",MISSING:"缺失",MISSING_DATA:"数据不足",MISSING_OR_STALE:"缺失或过期",STALE:"过期",EXPIRED:"过期",UNVERIFIED:"未核实",PARTIAL:"部分可用",AVAILABLE:"可用",PROXY:"代理指标",BACKGROUND_ONLY:"仅作背景",BACKGROUND:"仅作背景",BTC_GATE_INPUT:"BTC 门控输入",MACRO_GATE_CONTEXT:"宏观门控背景",CONTEXT_GATE:"资产风险门控",RULE:"规则",PATTERN:"共享数据的子模式",CONTEXT_OBSERVATION:"研究观察",THIS_OBSERVATION:"本观察使用",MATCHED:"已匹配",TRIGGERED_WATCH:"触发观察",TRIGGERED_ENTRY_CANDIDATE:"触发候选",TRIGGERED_RISK:"触发风险",NOT_TRIGGERED:"未触发",NOT_APPLICABLE:"不适用",MISSING_TIMESTAMP:"组件时点缺失",FUTURE:"未来时点",MISSING_OR_UNUSABLE:"缺失或不可用"}[value] || value);
  const source = r => `${r.source || "未记录来源"} · 观察 ${time(r.market_time)} · 可用 ${time(r.available_at)} · ID ${r.id || "未记录"}`;
  const refs = r => `<ul>${(r.sources || (r.source ? [r.source] : [])).map(ref => {
    const label = escapeHtml(source(ref));
    try {
      const url = new URL(ref.source_url);
      if (["https:", "http:"].includes(url.protocol)) return `<li><a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">${label}</a></li>`;
    } catch (_) { /* Missing or invalid source URLs stay plain text. */ }
    return `<li>${label}</li>`;
  }).join("")}</ul>`;
  const inputs = r => escapeHtml(JSON.stringify(r.inputs || {}));
  if (d.role === "LIFECYCLE") return `<h3>生命周期更新</h3><p>${escapeHtml(d.reason)} · ${time(d.as_of)}</p><p>原候选 ${escapeHtml(d.parent_signal_id)} · 原决策 ${time(d.parent_decision_as_of)}；本次没有产生新的看空信号。</p>`;
  return `<h3>本轮决策链 · ${time(d.as_of)}</h3><p>候选来源 ${escapeHtml(d.rule_id)} · 快照 ${escapeHtml(d.snapshot_id)} · 观察 ${time(d.market_time)} · 可用 ${time(d.available_at)}</p>
    <h3>触发与实际检查</h3>${list((d.triggered || []).map(r => `${r.rule_id} · ${status(r.role)} · ${status(r.status)}`))}
    <p>本规则与已匹配子模式输入覆盖 ${d.coverage?.usable_inputs ?? 0}/${d.coverage?.required_inputs ?? 0}；不是胜率或独立策略票数。</p>
    <h3>通过、限制与未知</h3>${list((d.gates || []).map(g => `${g.name}：${status(g.status)} ${(g.reasons || []).join("；")}`))}
    <p>BTC ${escapeHtml(d.btc_regime?.risk_mode || "未知")} · 宏观 ${escapeHtml(d.btc_regime?.macro || "未知")} · BTC 快照 ${escapeHtml(d.btc_regime?.snapshot_id || "未记录")}</p>
    ${d.entry_quality ? `<h3>入场质量</h3><p>${d.entry_quality.enabled ? escapeHtml(d.entry_quality.status) : "未启用"} · 双边近端深度下限 ${format(d.entry_quality.minimum_depth_usdt, 0)} USDT · 往返成本假设 ${format(d.entry_quality.round_trip_cost_bps)} bps</p>${list(d.entry_quality.reasons || [])}` : ""}
    ${d.market_confirmation ? `<h3>市场买盘确认</h3><p>${escapeHtml(d.market_confirmation.status)} · 其他币种有效覆盖 ${d.market_confirmation.eligible_assets}/${d.market_confirmation.expected_assets} · 买盘改善占比 ${format(d.market_confirmation.support_breadth_pct)}% · ${d.market_confirmation.mode === "observe" ? "研究对照" : "参与入场门控"}</p>${list(d.market_confirmation.limitations || [])}` : ""}
    <h3>缺失或不可用数据</h3>${list((d.data_gaps || []).map(g => `${g.scope === "OTHER_RULE" ? "其他规则" : g.scope === "THIS_RULE" ? "本规则" : "背景"} · ${g.rule_id || ""} ${g.field}：${status(g.status)}${g.market_time ? " · " + time(g.market_time) : ""}`))}${!d.data_gaps?.length ? "<p>本轮记录未标记缺项。</p>" : ""}
    <h3>资产背景</h3>${list(d.asset_context?.risks || [])}${(d.asset_context?.evaluations || []).map(r => `<p>${escapeHtml(r.kind)}：${escapeHtml(status(r.status))} · ${escapeHtml(status(r.role))}</p><p>${inputs(r)}</p>${refs(r)}${list(r.reasons || [])}`).join("")}
    ${currentContext?.risks?.length ? `<h3>当前发送检查 · ${time(currentContext.as_of)}</h3>${list(currentContext.risks)}<p>当前已暂停发送；上方保留候选产生时的证据。</p>` : ""}
    <h3>研究背景及来源</h3>${(d.background || []).map(r => `<details><summary>${escapeHtml(r.name)} · ${escapeHtml(status(r.status))} · ${escapeHtml(status(r.role))}</summary><p>${inputs(r)}</p>${list(r.evidence || r.limitations || r.reasons || [])}${refs(r)}</details>`).join("") || "<p>未传入可用评估或未启用。</p>"}
    <details><summary>全部规则实际评估记录</summary><div class="table-scroll"><table><thead><tr><th>规则/子模式</th><th>角色与结果</th><th>输入</th></tr></thead><tbody>${(d.evaluations || []).map(r => `<tr><td>${escapeHtml(r.rule_id)}</td><td>${escapeHtml(status(r.role))} · ${escapeHtml(status(r.status))}</td><td>${inputs(r)}</td></tr>`).join("")}</tbody></table></div></details>
    <h3>未用于本次判断</h3>${list(d.not_integrated || [])}<h3>发布检查与局限</h3><p>${escapeHtml(d.publication?.reason || "发布结果见预警级别；规则尚未完成收益验证")}</p>${list(d.limitations || [])}`;
}
async function showAlert(id) {
  try {
    const {alert:a,events,snapshot} = await (await api(`/api/alerts/${encodeURIComponent(id)}`)).json();
    $("#detail-title").textContent = `${a.symbol} · ${a.display_level || a.level}`;
    const list = (values) => `<ul>${values.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
    const action = (name,label) => `<button class="button secondary" data-alert-id="${escapeHtml(a.id)}" data-alert-action="${name}">${label}</button>`;
    $("#detail-body").innerHTML = `<p>${escapeHtml(a.title)} · ${escapeHtml(alertScores(a))}</p><p>规则 ${escapeHtml(a.rule_id)} · ${escapeHtml(a.rule_version)}</p>${a.patterns?.length ? `<h3>A–F 子模式</h3>${list(a.patterns)}` : ""}${a.context?.sources ? `<h3>研究证据时点</h3>${list(a.context.sources.map(r => `${r.source} · 观察 ${time(r.market_time)} · 可用 ${time(r.available_at)} · ${r.id}`))}` : ""}<p>首次发现 ${time(a.first_seen)} · 最新 ${time(a.last_updated)} · 当时价格 ${price(a.price)}</p>${decisionDetail(a.decision, a.current_asset_context)}${a.message_zh ? `<h3>本次提醒</h3>${list(a.message_zh.split("\n"))}` : ""}<div class="detail-grid"><div class="detail-item"><span>原候选参考价</span><strong>${price(a.original_candidate_price ?? a.notification_price)}</strong></div><div class="detail-item"><span>失效规则阈值（非支撑位）</span><strong>${price(a.original_invalidation_price ?? a.invalidation_price)}</strong></div><div class="detail-item"><span>最新报价</span><strong>${price(a.current_price)}</strong></div></div><p>最新报价时间 ${time(a.current_price_market_time)}${a.current_quote_fresh ? "" : " · 过期或缺失"}</p><h3>支持证据</h3>${list(a.evidence)}<h3>反向证据</h3>${list(a.contradictions)}<h3>失效条件</h3>${list(a.invalidation_conditions)}<h3>数据新鲜度</h3><p>报价 ${time(snapshot?.market_time)} · 详细 ${time(snapshot?.detail_updated_at)}</p><h3>状态变化</h3>${list(events.map((e) => `${time(e.event_at)} · ${e.action || e.reason} ${e.from_level || ""} → ${e.to_level || ""}`))}<p>${action(a.read_at ? "unread" : "read", a.read_at ? "标为未读" : "标为已读")} ${action(a.pinned ? "unpin" : "pin", a.pinned ? "取消固定" : "固定预警")} ${action("resolve","标为已解决")}</p><button class="button secondary" data-asset="${escapeHtml(a.asset_id)}">查看行情与原始依据</button>`;
    if (!$("#detail").open) $("#detail").showModal();
  } catch (e) {notice(e.message);}
}
["#rank-sort", "#alert-level", "#alert-state"].forEach((id) => $(id).addEventListener("change", renderV4));
$("#alert-search").addEventListener("input",renderV4);
$("#sound").addEventListener("click",async () => {
  soundEnabled = !soundEnabled;
  if (soundEnabled) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) {soundEnabled = false; notice("浏览器不支持声音提示");}
    else {audioContext ||= new Context(); await audioContext.resume();}
  }
  $("#sound").textContent = `声音提示：${soundEnabled ? "开启" : "关闭"}`;
});

function connectLive() {
  const connection = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live`);
  liveConnection = connection;
  connection.onopen = () => connection.send(JSON.stringify({token}));
  connection.onmessage = (event) => {if (JSON.parse(event.data).type === "update") loadOverview();};
  connection.onclose = () => {liveConnection = null; setTimeout(() => {if (!liveConnection && overview) connectLive();}, 5000);};
}
