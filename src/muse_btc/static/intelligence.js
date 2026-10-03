"use strict";
let intelligenceData = null;
let intelligenceTab = "research";
let intelligenceBusy = false;

function sourceLink(url, label = "原始来源 ↗") {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") return "";
    return `<a href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch (_) { return ""; }
}
function intelTable(headers, rows) {
  return rows.length ? `<div class="table-scroll"><table><thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(cells => `<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : '<p class="empty">尚无已归档数据。请在云端采集或导入。</p>';
}
async function loadIntelligence() {
  if (intelligenceBusy) return;
  intelligenceBusy = true;
  try {
    intelligenceData = await (await api("/api/intelligence")).json();
    renderIntelligence();
  } catch (error) { notice(error.message); }
  finally { intelligenceBusy = false; }
}
function renderIntelligence() {
  if (!intelligenceData) return;
  const d = intelligenceData;
  let html = "";
  if (intelligenceTab === "research") {
    html = '<p>检查从上一次成功检查起的新研究。首次检查建立基线；中文结论与原文证据审阅后才进入提醒。普通新闻、营销与重复观点不提醒。</p><div class="actions"><button class="button primary" data-intel-action="research">手动检查研究来源</button></div>';
    html += intelTable(["研究 / 来源", "发表时间", "质量 / 增量", "处理"], d.research_documents.map(r => [
      `${escapeHtml(r.data.title)}<small>${escapeHtml(r.source)}</small>${sourceLink(r.data.source_url)}`,
      time(r.data.published_at), `${escapeHtml(r.data.quality)}<small>${escapeHtml(r.data.novelty)}</small>`,
      `<button class="button secondary" data-research-review="${escapeHtml(r.id)}">原文与中文审阅</button>`]));
    html += '<h3>重要研究提醒</h3>' + d.research_notices.map(n => `<article class="signal-row"><strong>${escapeHtml(n.title_zh)}</strong><small>${escapeHtml(n.institution)} · ${time(n.published_at)}</small><p>${escapeHtml(n.core_conclusion)}</p><p>数据 / 方法：${escapeHtml(n.key_data_method)}</p><p>交易意义（研究推断）：${escapeHtml(n.trading_implication)}</p><p>局限：${escapeHtml(n.limitations)}</p>${sourceLink(n.original_source)}</article>`).join("");
    html += '<h3>检查记录</h3>' + intelTable(["来源", "时间", "结果"], d.research_checks.map(r => [escapeHtml(r.source), time(r.checked_at), escapeHtml(r.message)]));
  } else if (intelligenceTab === "macro") {
    const m = d.macro;
    html = '<div class="actions"><button class="button primary" data-intel-action="macro">手动采集宏观 / 资金流</button></div>';
    html += `<p>10Y–2Y：${pct(m.yield_spread_10y_2y_pct)} · 净流动性代理：${price(m.net_liquidity_proxy_usd)} · 四币供应变化均值：${pct(m.crypto_liquidity_index)}</p>`;
    html += intelTable(["序列", "最新值", "观察日 / 获取时间", "来源"], m.series.map(r => [escapeHtml(r.key), format(r.data.value), `${time(r.market_time)} / ${time(r.available_at)}`, sourceLink(r.data.source_url)]));
    html += '<h3>稳定币</h3>' + intelTable(["资产", "供应", "7 日变化", "30 日变化"], m.stablecoins.map(r => [escapeHtml(r.data.symbol), price(r.data.supply_usd), pct(r.data.change_7d_pct), pct(r.data.change_30d_pct)]));
    html += '<h3>ETF</h3>' + intelTable(["日期", "资产", "净流入"], m.etf.map(r => [time(r.market_time), escapeHtml(r.data.asset), price(r.data.net_flow_usd)]));
    html += '<h3>宏观事件</h3><button class="button secondary" data-intel-action="events">检查官方日历与发布值</button><button class="button secondary" id="macro-event-import">导入事件</button>';
    html += intelTable(["事件", "发布时间", "状态"], (d.events || []).map(r => [escapeHtml(r.data.name), time(r.data.release_time), escapeHtml(r.data.status)]));
    html += intelTable(["已发布事件", "实际 / 预期", "+5m", "+15m", "+1h", "+4h", "+24h"], (d.event_reactions || []).map(r => [escapeHtml(r.event.data.name), `${format(r.event.data.actual)} / ${format(r.event.data.consensus)}`, ...[300,900,3600,14400,86400].map(t => r.btc_reactions[t] ? `${pct(r.btc_reactions[t].return_from_release_pct)}<small>偏移 ${r.btc_reactions[t].offset_seconds}s</small>` : "数据缺失或未到期")]));
    if (d.event_checks) html += intelTable(["来源", "结果", "原因"], d.event_checks.sources.map(r => [escapeHtml(r.source), escapeHtml(r.status), escapeHtml(r.reason || "")]));
    html += `<p>缺失：${escapeHtml(m.missing.join("、"))}</p><p>过期：${escapeHtml(m.stale.join("、"))}</p>`;
  } else if (intelligenceTab === "btc") {
    const b = d.btc;
    html = '<p>手动归档免费来源。持有人成本与 SOPR 延迟七天；期权仅覆盖 Deribit，清算为监听期间的采样。</p><div class="actions"><button class="button primary" data-btc-scope="all">采集免费数据</button><button class="button secondary" data-btc-scope="onchain">仅采集链上</button><button class="button secondary" data-btc-scope="options">仅采集期权</button><button class="button secondary" data-btc-scope="liquidations">监听清算 30 秒</button></div>';
    const assessment = d.btc_assessment;
    if (assessment) {
      const labels = {spot_demand:"现货需求",leverage_risk:"杠杆风险",macro_liquidity:"宏观流动性",etf_flow:"ETF 资金流",stablecoin_supply:"稳定币供应",onchain_valuation_percentile:"链上估值分位数",options_iv:"期权波动率",research_bias:"研究观点"};
      html += `<h3>BTC 综合研判</h3><p>证据覆盖 ${format(assessment.coverage_pct)}% · 宏观背景 ${escapeHtml(assessment.macro_risk)} · 分数为规则描述，置信度尚未校准。</p>`;
      html += intelTable(["维度", "分值 / 状态", "依据"], Object.entries(assessment.dimensions).map(([k,v]) => [escapeHtml(labels[k] || k), `${format(v.score)} / ${escapeHtml(v.status)}`, escapeHtml(v.evidence.join("；"))]));
    }
    html += '<h3>BTC 链上</h3>' + intelTable(["指标 / 来源", "最新值 / 单位", "观察日期", "状态 / 历史点数"], b.onchain.map(r => [
      `${escapeHtml(r.metric)}<small>${escapeHtml(r.source)}</small>${sourceLink(r.source_url)}`,
      `${format(r.value,4)} ${escapeHtml(r.unit)}`, time(r.observation_date), `${escapeHtml(r.status)} / ${r.observations}`]));
    const o = b.options;
    html += '<h3>BTC 期权</h3>';
    if (o) {
      html += `<p>${escapeHtml(o.status)} · 归档 ${o.data.chain.length} 项 · Greeks ${o.data.greeks_observed}/${o.data.chain.length} · 获取于 ${time(o.available_at)}</p>`;
      html += intelTable(["合约 / 到期", "Mark IV", "OI（BTC）", "Delta / Gamma / Vega"], o.data.chain.slice(0,80).map(r => [
        `${escapeHtml(r.instrument)}<small>${time(r.expiry)}</small>`, pct(r.mark_iv_pct), format(r.open_interest_btc,4),
        r.greeks ? `${format(r.greeks.delta,4)} / ${format(r.greeks.gamma,6)} / ${format(r.greeks.vega,4)}` : "未采样"]));
      if (o.data.chain.length > 80) html += '<p>表格展示前 80 项。</p>';
    } else html += '<p class="empty">尚无已归档期权数据。</p>';
    html += '<h3>采样清算</h3>' + intelTable(["资产 / 被清算方向", "快照累计成交额（USDT）", "成交时间"], b.liquidations.events.map(r => [
      `${escapeHtml(r.data.symbol)} / ${escapeHtml(r.data.liquidated_position)}`, format(r.data.snapshot_filled_notional_quote,2), time(r.market_time)]));
    html += intelTable(["监听状态", "开始 / 结束", "实际秒数 / 新事件"], b.liquidations.windows.map(r => [
      escapeHtml(r.data.status), `${time(r.data.connected_at)} / ${time(r.data.ended_at)}`, `${format(r.data.connected_seconds,1)} / ${r.data.new_events}`]));
    html += '<h3>来源检查</h3>' + intelTable(["来源 / 指标", "结果", "时间 / 原因"], b.checks.map(r => [
      `${escapeHtml(r.source)} / ${escapeHtml(r.metric || "liquidations")}`, `${escapeHtml(r.status)} ${escapeHtml(r.data_status || "")}`, `${time(r.checked_at)}<small>${escapeHtml(r.reason || r.error || (r.errors || []).map(e => `${e.instrument}: ${e.reason}`).join("；"))}</small>`]));
    html += `<p>${escapeHtml(b.limitations.join("；"))}</p><p>宏观、稳定币和 ETF 结果在“宏观与资金流”查看。</p>`;
  } else if (intelligenceTab === "meme") {
    html = `<p>独立发现池不占 100 币名额。采集开关：${d.meme_collection_enabled ? "已启用" : "未启用"}。公开 profile 覆盖有限，未知风险不视为安全。</p><button class="button primary" data-intel-action="meme">采集一批发现池</button>`;
    html += intelTable(["资产 / 合约", "发现评分", "Rug 风险", "流动性", "首次发现 / 涨幅"], d.meme.tokens.map(r => [
      `${escapeHtml(r.data.symbol)}<small>${escapeHtml(r.key)}</small>`, format(r.data.discovery_score), `${escapeHtml(r.data.risk_status)} · ${format(r.data.rug_risk_score)}`, price(r.data.liquidity_usd), `${time(r.data.first_detected)} / ${pct(r.data.return_since_detection_pct)}`]));
    html += '<h3>持有人</h3>' + intelTable(["Token", "观察地址", "Top 10", "Top 20", "覆盖"], d.meme.holders.map(r => [escapeHtml(r.token), format(r.observed_unique_holders,0), pct(r.top10_pct), pct(r.top20_pct), r.complete ? "完整导入" : "部分导入"]));
    html += '<h3>钱包历史</h3>' + intelTable(["钱包", "已关闭批次", "收益中位数", "身份"], d.meme.wallets.map(r => [escapeHtml(r.wallet), format(r.closed_lots,0), pct(r.median_return_pct), escapeHtml(r.identity_status)]));
  } else if (intelligenceTab === "social") {
    const s = d.social;
    html = `<p>1h 观察贴文 ${s.observed_posts_1h} · 独立作者 ${s.unique_authors} · 提及变化 ${s.mention_acceleration} · 重复文本比例 ${format(s.text_similarity_repeat_ratio)}</p><button class="button primary" data-intel-action="social">手动读取 X 数据</button>`;
    html += intelTable(["叙事", "提及数", "作者", "变化", "Token / Leader"], s.narratives.map(r => [escapeHtml(r.narrative), format(r.mentions_1h,0), format(r.unique_authors,0), format(r.acceleration,0), `${r.token_count} / ${escapeHtml(r.leader || "待关联")}`]));
    html += '<h3>KOL 观点验证</h3>' + intelTable(["作者", "成熟观点", "方向命中率", "状态"], s.kol_reputation.map(r => [escapeHtml(r.author_id), format(r.measured_theses,0), format(r.direction_hit_rate), escapeHtml(r.reputation_status)]));
    html += `<p>${escapeHtml(s.limitations.join("；"))}</p>`;
  } else {
    html = '<p>模型只从归档数据训练，按时间隔离训练、验证与测试。样本不足返回明确状态；训练结果不会自动改规则或启用交易。</p><div class="actions"><button class="button secondary" data-intel-report>TopK / Lead Time 报告</button><button class="button primary" data-intel-train>训练 4h 研究模型</button></div>';
    html += intelTable(["模型", "状态", "样本", "测试 Brier"], d.models.map(r => [escapeHtml(r.id), escapeHtml(r.data.status), escapeHtml(JSON.stringify(r.data.samples)), format(r.data.test_brier_score,4)]));
    html += '<pre id="experiment-output" class="intelligence-json"></pre>';
  }
  if (d.runtime) {
    html += `<h3>采集运行状态</h3><p>扩展采集：${escapeHtml(d.runtime.worker.status)} · 最近心跳 ${time(d.runtime.worker.heartbeat_at)}</p>`;
    html += intelTable(["任务", "状态", "最近成功", "完成时间"], d.runtime.jobs.map(r => [escapeHtml(r.name), escapeHtml(r.status), time(r.last_success_at), time(r.finished_at)]));
  }
  $("#intelligence-content").innerHTML = html;
  $$("[data-intel-tab]").forEach(b => b.classList.toggle("selected", b.dataset.intelTab === intelligenceTab));
}
function showJsonEditor(title, value, onSave, source = null) {
  $("#detail-title").textContent = title;
  $("#detail-body").innerHTML = `${source ? `<details><summary>原文</summary><pre class="intelligence-source">${escapeHtml(source)}</pre></details>` : ""}<label>填写有来源的 JSON<textarea id="intelligence-editor" rows="20" aria-label="证据 JSON">${escapeHtml(JSON.stringify(value,null,2))}</textarea></label><button class="button primary" id="intelligence-save">提交</button>`;
  $("#detail").showModal();
  $("#intelligence-save").addEventListener("click", async () => {
    try {
      const payload = JSON.parse($("#intelligence-editor").value);
      const result = await onSave(payload);
      $("#detail").close(); notice(JSON.stringify(result)); await loadIntelligence();
    } catch (error) { notice(error.message); }
  });
}
document.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.id === "macro-event-import") {
      $("#detail-title").textContent = "导入已发布宏观事件";
      $("#detail-body").innerHTML = '<form id="macro-form"><label>事件名称<input name="name" required></label><label>发布时间（本地时区）<input name="release" type="datetime-local" required></label><label>实际值<input name="actual" type="number" step="any" required></label><label>市场预期（可空）<input name="consensus" type="number" step="any"></label><label>单位<input name="units" required></label><label>原始 HTTPS 来源<input name="source" type="url" required></label><button class="button primary" type="submit">保存事件</button></form>';
      $("#detail").showModal();
      $("#macro-form").addEventListener("submit", async event => {
        event.preventDefault();
        try {
          const f = new FormData(event.target);
          const release = new Date(f.get("release")).toISOString();
          const data = {name:f.get("name"),release_time:release,actual:Number(f.get("actual")),consensus:f.get("consensus") === "" ? null : Number(f.get("consensus")),units:f.get("units"),vintage:"MANUAL_IMPORT"};
          await api("/api/context/import", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:"macro_event",key:`${data.name}:${release}`,source_url:f.get("source"),market_time:release,data})});
          $("#detail").close(); await loadIntelligence();
        } catch (error) { notice(error.message); }
      });
    }
    if (button.dataset.intelTab) { intelligenceTab = button.dataset.intelTab; renderIntelligence(); }
    if (button.dataset.btcScope) {
      button.disabled = true;
      const result = await (await api("/api/btc/collect", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scope:button.dataset.btcScope,liquidation_seconds:button.dataset.btcScope === "liquidations" ? 30 : 10})})).json();
      notice(JSON.stringify(result)); await loadIntelligence();
    }
    if (button.dataset.intelAction) {
      const scope = button.dataset.intelAction;
      button.disabled = true;
      const endpoint = scope === "research" ? "/api/research/check" : `/api/intelligence/collect/${scope}`;
      const result = await (await api(endpoint, {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();
      notice(JSON.stringify(result)); await loadIntelligence();
    }
    if (button.dataset.researchReview) {
      const doc = await (await api(`/api/research/documents/${encodeURIComponent(button.dataset.researchReview)}`)).json();
      showJsonEditor("中文研究审阅", {title_zh:"",core_conclusion:"",key_data_method:"",trading_implication:"",limitations:"",incremental_information:"",original_excerpts:[],assets:["BTC","ETH"],direction:"NEUTRAL"},
        async payload => (await api(`/api/research/documents/${encodeURIComponent(doc.id)}/review`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json(), doc.data.body);
    }
    if (button.id === "context-import") showJsonEditor("导入来源证据", {kind:"catalyst",key:"",source_url:"",market_time:new Date().toISOString(),data:{asset_id:"",title:"",event_time:new Date().toISOString(),category:"other",direction:"UNKNOWN",verified:false,confidence:0}}, async payload => (await api("/api/context/import",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json());
    if (button.hasAttribute("data-intel-report")) $("#experiment-output").textContent = JSON.stringify(await (await api("/api/experiments/report")).json(),null,2);
    if (button.hasAttribute("data-intel-train")) $("#experiment-output").textContent = JSON.stringify(await (await api("/api/experiments/train",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({horizon_seconds:14400})})).json(),null,2);
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});
