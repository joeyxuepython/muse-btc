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
  if (["RISK", "INVALIDATED"].includes(s.kind)) return "风险等级：重要风险";
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
        `<article class="signal-row" data-signal="${escapeHtml(s.id)}" tabindex="0"><div class="signal-top">${tag(kinds[s.kind] || s.kind, ["RISK", "INVALIDATED"].includes(s.kind) ? "danger" : s.kind === "WATCH" ? "warning" : "")}<strong>${escapeHtml(s.symbol)}</strong><time>${time(s.emitted_at)}</time></div><p>${escapeHtml(s.title)}</p><div class="signal-meta"><span>${escapeHtml(states[s.state] || s.state)}</span><span>${escapeHtml(signalScore(s))}</span><span>${s.horizon_seconds / 60}m 观察窗</span><span>${escapeHtml(s.rule_id)}</span></div></article>`,
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
      a.state === "ACTIVE" && ["STRONG", "CRITICAL_RISK"].includes(a.level)))
      .filter((g) => g.members.some((a) => !seenAlerts.has(a.id + ":" + (a.notification_id || a.notification_revision || a.first_seen))))
      .forEach((g) => {
        const a = g.primary;
        if (notificationsEnabled) new Notification(`${a.symbol} · ${a.level}`, {body: g.members.map(m => m.title).join("；"), tag: g.key});
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
async function showSignal(id) {
  try {
    const { signal: s, outcomes } = await (
      await api(`/api/signals/${encodeURIComponent(id)}`)
    ).json();
    $("#detail-title").textContent = s.symbol + " · " + s.title;
    const list = (values) =>
      `<ul>${values.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
    $("#detail-body").innerHTML =
      `<p>${tag(kinds[s.kind])} ${tag(states[s.state] || s.state)} ${tag("规则观察期", "warning")}</p><div class="detail-grid"><div class="detail-item"><span>触发时价格</span><strong>${price(s.reference_price)}</strong></div><div class="detail-item"><span>失效参考位</span><strong>${price(s.invalidation_price)}</strong></div><div class="detail-item"><span>入场参考区间</span><strong>${s.entry_zone ? s.entry_zone.map(price).join(" — ") : "尚未满足入场条件"}</strong></div><div class="detail-item"><span>有效至 UTC+8</span><strong>${time(s.expires_at)}</strong></div></div><h3>触发证据</h3>${list(s.evidence)}<h3>反向证据与限制</h3>${list(s.contradictions.length ? s.contradictions : ["规则尚未完成充分的样本外验证"])}<h3>失效条件</h3>${list(s.invalidation_conditions)}<h3>历史状态</h3>${list(s.events.map((e) => time(e.event_at) + " · " + (states[e.state] || e.state) + " · " + e.reason))}<h3>事后观察</h3>${outcomes.length ? list(outcomes.map((o) => `${o.horizon_seconds / 60}m：价格变化 ${pct(o.return_pct)}，最大不利变化 ${pct(o.max_adverse_pct)}，${o.sample_count} 次采样`)) : "<p>尚未到达观察时间窗，或缺少覆盖该时间窗的真实价格。</p>"}<p class="muted">规则 ${escapeHtml(s.rule_id)} / ${escapeHtml(s.rule_version)} · ${s.evidence_groups.map(escapeHtml).join(" + ")}</p><button class="button secondary" data-asset="${escapeHtml(s.asset_id)}">查看标的原始依据 ↗</button>`;
    if (!$("#detail").open) $("#detail").showModal();
  } catch (e) {
    notice(e.message);
  }
}
async function loadValidation() {
  try {
    const r = await (await api("/api/validation")).json();
    $("#validation-body").innerHTML =
      `<div class="validation-summary"><div><strong>${r.signal_count}</strong><small>归档提醒</small></div><div><strong>${r.outcome_count}</strong><small>已测量时间窗</small></div></div>${r.rules.length ? `<div class="table-scroll"><table><thead><tr><th>规则</th><th>时间窗</th><th>测量 / 覆盖样本</th><th>平均价格变化</th><th>平均不利变化</th></tr></thead><tbody>${r.rules.map((row) => `<tr><td>${escapeHtml(row.rule_id)}</td><td>${row.horizon_seconds / 60}m</td><td>${row.measured_count} / ${row.covered_count}</td><td>${pct(row.mean_return_pct)}</td><td>${pct(row.mean_max_adverse_pct)}</td></tr>`).join("")}</tbody></table></div>` : "<p>尚无已完成的前瞻观察。系统将在 15m、1h、4h、24h、3d、7d、14d、30d 时间窗积累真实结果。</p>"}<ul>${r.limitations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
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
    const separate = a.level === "CRITICAL_RISK" || a.context?.validation_status === "OBSERVATION_ONLY" || !["ACTIVE", "PAUSED"].includes(a.state);
    const key = separate ? a.id : `${a.asset_id}:${a.state}:${a.horizon_seconds || 3600}`;
    if (!groups.has(key)) groups.set(key, {key, members:[]});
    groups.get(key).members.push(a);
  }
  return [...groups.values()].map(g => {
    g.members.sort((a,b) => (levels[b.level]-levels[a.level]) || b.last_updated.localeCompare(a.last_updated));
    g.primary = g.members[0];
    g.patterns = [...new Set(g.members.flatMap(a => a.patterns || []))];
    return g;
  });
}
function alertScores(a) {
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
  const alerts = (overview.alerts || []).filter((a) => (!level || a.level === level) &&
    (!state || (state === "unread" ? a.unread : state === "pinned" ? a.pinned : a.state === state)) &&
    `${a.symbol} ${a.title} ${a.rule_id}`.toLowerCase().includes(query));
  $("#web-alerts").innerHTML = alerts.length ? groupAlerts(alerts).map((g) => {
    const a = g.primary;
    return `<article class="signal-row" data-alert="${escapeHtml(a.id)}" tabindex="0"><div class="signal-top">${tag(a.level, a.level === "CRITICAL_RISK" ? "danger" : "warning")}<strong>${escapeHtml(a.symbol)}</strong>${g.members.some(m => m.unread) ? tag("未读") : ""}${g.members.some(m => m.pinned) ? tag("已固定") : ""}<time>${time(a.last_updated)}</time></div><p>${g.members.map(m => escapeHtml(m.title)).join("；")}</p><div class="signal-meta"><span>${escapeHtml(alertScores(a))}</span><span>${escapeHtml(states[a.state] || a.state)}</span><span>首次 ${time(a.first_seen)}</span></div>${g.patterns.length ? `<p>${escapeHtml(g.patterns.join("；"))}</p>` : ""}${g.members.length > 1 ? '<p>同币相关条件合并，分数不相加，不能视作多套独立策略确认。</p>' : ""}<div class="actions">${g.members.map(m => `<button class="button secondary" data-alert="${escapeHtml(m.id)}">${escapeHtml(m.rule_id)}${m.rule_evidence_score != null ? ` · 证据 ${format(m.rule_evidence_score)}` : ""}</button>`).join("")}</div></article>`;
  }).join("") : '<p class="empty">暂无符合条件的真实预警。</p>';
}
async function showAlert(id) {
  try {
    const {alert:a,events,snapshot} = await (await api(`/api/alerts/${encodeURIComponent(id)}`)).json();
    $("#detail-title").textContent = `${a.symbol} · ${a.level}`;
    const list = (values) => `<ul>${values.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
    const action = (name,label) => `<button class="button secondary" data-alert-id="${escapeHtml(a.id)}" data-alert-action="${name}">${label}</button>`;
    $("#detail-body").innerHTML = `<p>${escapeHtml(a.title)} · ${escapeHtml(alertScores(a))}</p><p>规则 ${escapeHtml(a.rule_id)} · ${escapeHtml(a.rule_version)}</p>${a.patterns?.length ? `<h3>A–F 子模式</h3>${list(a.patterns)}` : ""}${a.context?.sources ? `<h3>研究证据时点</h3>${list(a.context.sources.map(r => `${r.source} · 观察 ${time(r.market_time)} · 可用 ${time(r.available_at)} · ${r.id}`))}` : ""}<p>首次发现 ${time(a.first_seen)} · 最新 ${time(a.last_updated)} · 当时价格 ${price(a.price)}</p><h3>支持证据</h3>${list(a.evidence)}<h3>反向证据</h3>${list(a.contradictions)}<h3>失效条件</h3>${list(a.invalidation_conditions)}<h3>数据新鲜度</h3><p>报价 ${time(snapshot?.market_time)} · 详细 ${time(snapshot?.detail_updated_at)}</p><h3>状态变化</h3>${list(events.map((e) => `${time(e.event_at)} · ${e.action || e.reason} ${e.from_level || ""} → ${e.to_level || ""}`))}<p>${action(a.read_at ? "unread" : "read", a.read_at ? "标为未读" : "标为已读")} ${action(a.pinned ? "unpin" : "pin", a.pinned ? "取消固定" : "固定预警")} ${action("resolve","标为已解决")}</p><button class="button secondary" data-asset="${escapeHtml(a.asset_id)}">查看行情与原始依据</button>`;
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
