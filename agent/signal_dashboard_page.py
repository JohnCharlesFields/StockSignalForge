"""HTML page for the embedded three-layer research dashboard."""

from __future__ import annotations


def signal_dashboard_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>easymoneysniper · 三维信号总览</title>
  <style>
    :root{color-scheme:dark;--bg:#09131b;--panel:#101e29;--line:#263947;--muted:#91a5b2;--text:#eef5f7;--good:#6dd7b2;--wait:#e9b85d;--bad:#f07b78;--gold:#d5ad5c;--cyan:#55c5b5}
    *{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:13px/1.45 Inter,"Microsoft YaHei",sans-serif}
    h1{margin:0;font-size:22px}p{margin:5px 0 12px;color:var(--muted)}.brand{display:flex;align-items:center;gap:9px;color:var(--gold);font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:7px}
    .ball{display:inline-grid;place-items:center;width:24px;height:24px;border:2px solid var(--gold);border-radius:50%;font-size:9px;letter-spacing:0}
    .flow{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.flow span{padding:6px 9px;border:1px solid var(--line);border-radius:5px;background:#102532;color:#bcd7db;font-size:12px}
    .toolbar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}select,button{min-height:40px;padding:9px 13px;border:1px solid var(--line);border-radius:6px;background:#152632;color:var(--text);white-space:nowrap}select{min-width:420px;max-width:100%}.limit-select{min-width:110px;width:110px}button{cursor:pointer;background:#16635e;font-weight:700}button.secondary{background:#152632}button.agent{background:#8a5b18;border-color:#b47d2d}button:disabled{opacity:.55;cursor:not-allowed}
    .pool-note{width:100%;color:var(--muted);font-size:12px}.progress{display:none;margin:0 0 14px;padding:12px 14px;border:1px solid var(--line);border-radius:7px;background:#102532}.progress.show{display:block}.progress-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px}.bar{height:7px;border-radius:9px;background:#203641;overflow:hidden}.bar i{display:block;height:100%;background:var(--cyan);transition:width .25s}.summary{margin-top:8px;color:var(--muted);font-size:12px}
    .macro-banner{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:8px;margin:0 0 14px}.macro-card{border:1px solid var(--line);border-radius:7px;background:#102532;padding:10px}.macro-card span{display:block;color:var(--muted);font-size:11px}.macro-card b{display:block;margin-top:2px}.macro-card.good b{color:var(--good)}.macro-card.wait b{color:var(--wait)}.macro-card.bad b{color:var(--bad)}
    .best-pick{display:none;margin:0 0 14px;border:1px solid #3a6f5e;border-left:4px solid var(--good);border-radius:8px;background:#102b24;padding:12px 14px}.best-pick.show{display:block}.best-pick h2{margin:0 0 6px;font-size:15px;color:#dffcf0}.best-pick .line{display:flex;flex-wrap:wrap;gap:8px 14px;color:#cde8df}.best-pick .line b{color:#fff}.best-pick small{display:block;margin-top:6px;color:#a9c8bf}.best-badge{display:inline-block;margin-left:6px;padding:1px 6px;border:1px solid #4aa987;border-radius:999px;background:#153f35;color:#a9f0d4;font-size:11px}
    .llm-review{margin-top:10px;border:1px solid #2d5e82;border-radius:7px;background:#10283a;padding:10px}.llm-review b{display:block;color:#cdeaff}.llm-review small{display:block;color:#a9c8d8}
    .panel{border:1px solid var(--line);border-radius:7px;background:var(--panel);overflow-y:auto;overflow-x:hidden;max-height:calc(100vh - 250px)}
    table{width:100%;border-collapse:collapse;table-layout:fixed;min-width:0}th,td{padding:7px 6px;border-bottom:1px solid #20333f;text-align:left;vertical-align:top;overflow-wrap:anywhere;word-break:break-word}th{color:#b9cbd3;background:#12232e;font-size:11px;position:sticky;top:0;z-index:1}
    tr.highlight{background:#173528!important;border-left:3px solid var(--good)}tr.best-row,#rows tr:first-child{background:#143d31!important;border-left:4px solid var(--good);box-shadow:inset 0 0 0 1px rgba(109,215,178,.25)}#rows tr:first-child:has(td.empty){background:transparent!important;border-left:0;box-shadow:none}.lamp{display:flex;align-items:center;gap:5px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}.dot.good{background:var(--good)}.dot.wait{background:var(--wait)}.dot.bad{background:var(--bad)}
    .muted{color:var(--muted)}.stack{display:grid;gap:4px}.stack b,.stack small{display:block}.stack small{color:var(--muted);line-height:1.32}.score{color:#82e4c4;font-size:15px}.pe-positive{color:var(--good);font-weight:700}.pe-loss{color:var(--bad);font-weight:800}.pe-na{color:var(--muted)}
    th.sortable{cursor:pointer;user-select:none}th.sortable:hover{color:#fff;background:#183140}.sort-mark{color:var(--cyan);font-size:10px;margin-left:3px}.tag{display:inline-block;padding:2px 6px;border:1px solid #395666;border-radius:999px;color:#bfd4dc;font-size:11px}.tag.good{border-color:#34755f;color:#9de5ca}.tag.wait{border-color:#7b6232;color:#f2ce7c}.tag.bad{border-color:#7a3d42;color:#ffaaa8}.relay b{color:#f1d48e}.gex{border-left:3px solid #395666;padding-left:7px}.gex.bad{border-left-color:var(--bad)}.gex.good{border-left-color:var(--good)}.gex.wait{border-left-color:var(--wait)}
    .spark{width:100%;height:30px;display:block}.trend-up{color:var(--good)}.trend-down{color:var(--bad)}
    a{color:#8fc0ff;text-decoration:none}.link-btn{display:inline-block;border:0;background:transparent;color:#8fc0ff;padding:0;min-height:0;font:inherit;font-weight:500}.empty{padding:28px;text-align:center;color:var(--muted)}.notice{margin:12px 2px 0;color:var(--muted);font-size:12px}
    .detail-panel{display:none;margin:12px 0;border:1px solid var(--line);border-radius:7px;background:#0f202b;padding:14px}.detail-panel.show{display:block}.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px}.kv{border:1px solid #213744;border-radius:6px;background:#142631;padding:8px}.kv span{display:block;color:var(--muted);font-size:11px}.kv b{display:block;margin-top:2px}.detail-section{margin-top:12px}.detail-section b{display:block;margin-bottom:5px}
    @media(max-width:1000px){body{padding:14px;font-size:12px}th,td{padding:7px 5px}.panel{max-height:calc(100vh - 235px)}select{min-width:220px}}
  </style>
</head>
<body>
  <div class="brand"><span class="ball">35</span> easymoneysniper · research board</div>
  <h1>三维量化信号总览</h1>
  <p>按研究胜率口径排序：历史验证胜率优先，其次统一研究概率，再看风险收益比与可执行性。股票池下拉框也按同一口径动态排序，最高分池默认展示。</p>
  <div class="flow">
    <span>第一层 · 机会质量</span>
    <span>第二层 · 启动择时 + 日隧道协商</span>
    <span>第三层 · 事件风险 + Gamma Exposure</span>
    <span>汇总 · 统一证据与失效退出</span>
  </div>
  <div class="toolbar">
    <select id="universe"></select>
    <select id="rowLimit" class="limit-select"><option value="20" selected>Top 20</option><option value="50">Top 50</option><option value="100">Top 100</option><option value="200">Top 200</option><option value="500">All</option></select>
    <button class="agent" id="run">运行三层分析</button>
    <button class="secondary" id="load">刷新总览</button>
    <div class="pool-note" id="poolNote"></div>
  </div>
  <div class="macro-banner" id="macroBanner"></div>
  <div class="best-pick" id="bestPick"></div>
  <div class="progress" id="progress">
    <div class="progress-head"><b id="phase">准备运行</b><span id="pct">0%</span></div>
    <div class="bar"><i id="bar" style="width:0%"></i></div>
    <div class="summary" id="summary">按股票池依次更新机会筛选、启动择时、事件风险，并生成统一决策清单。</div>
  </div>
  <div class="panel">
    <table>
      <colgroup>
        <col style="width:8%"><col style="width:10%"><col style="width:14%"><col style="width:10%"><col style="width:9%">
        <col style="width:14%"><col style="width:11%"><col style="width:9%"><col style="width:15%">
      </colgroup>
      <thead id="head"></thead>
      <tbody id="rows"><tr><td colspan="9" class="empty">正在载入...</td></tr></tbody>
    </table>
  </div>
  <div class="detail-panel" id="detailPanel"><div class="empty">点击表格中的“详情”后按需加载完整解释、评分拆解、事件、GEX 和日隧道协商。</div></div>
  <div class="notice">排序分是研究胜算分，不等于实盘确定胜率；历史样本不足时，会降级使用同行接力代理、统一概率代理或 Gamma 风险惩罚。</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct=v=>v==null?"--":(Number(v||0)*100).toFixed(1)+"%";
const winText=w=>w&&w.recent_win_score!=null?`胜算 ${pct(w.recent_win_score)}`:"胜率待验证";
const money=v=>v==null?"--":"$"+Number(v).toFixed(2);
const cap=v=>{let n=Number(v||0);return !n?"--":n>=1e12?"$"+(n/1e12).toFixed(1)+"T":n>=1e9?"$"+(n/1e9).toFixed(1)+"B":"$"+(n/1e6).toFixed(0)+"M"};
const pe=v=>{if(v==null||v==="")return '<span class="pe-na">--</span>';let n=Number(v);if(!Number.isFinite(n))return '<span class="pe-na">--</span>';if(n<0)return '<span class="pe-loss">亏损</span>';return `<span class="pe-positive">${n.toFixed(1)}x</span>`};
function dot(ok,label){let tone=ok===true?"good":ok===false?"bad":"wait";return `<span class="lamp"><i class="dot ${tone}"></i>${esc(label)}</span>`}
function getPoolType(u){if(u==="pre_earnings_revision")return"pre";if(u==="speculative_peer_earnings")return"peer";return"std"}
function spark(values){let vals=(values||[]).map(Number).filter(v=>Number.isFinite(v)&&v>0);if(vals.length<2)return '<span class="muted">--</span>';let mn=Math.min(...vals),mx=Math.max(...vals),span=mx-mn||1,pts=vals.map((v,i)=>`${(i/(vals.length-1)*100).toFixed(1)},${(28-(v-mn)/span*24).toFixed(1)}`).join(" ");let up=vals[vals.length-1]>=vals[0];return `<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${up?"#6dd7b2":"#f07b78"}" stroke-width="2"/></svg>`}
function trendCell(o){let trend=Number(o.trend_30d||0),cls=trend>=0?"trend-up":"trend-down";return `<div class="stack">${spark(o.recent_closes)}<small class="${cls}">30日 ${pct(trend)}</small><small>现价 ${money(o.spot)}</small></div>`}
function gexTone(g){let risk=String(g?.risk_level||"").toLowerCase(),regime=String(g?.regime||"").toLowerCase();if(risk==="high"||regime==="negative_high")return"bad";if(regime==="positive")return"good";return"wait"}
function gexHtml(g,compact=false){
  if(!g||!g.available)return compact?'<span class="tag wait">GEX --</span>':'<div class="gex wait"><small>Gamma Exposure 暂无可用期权链/OI，不能作为风控证据。</small></div>';
  let tone=gexTone(g),title=`GEX ${String(g.risk_level||"--").toUpperCase()} / ${esc(g.regime||"--")}`;
  if(compact)return `<span class="tag ${tone}">${title}</span>`;
  let parts=[];
  if(g.gamma_flip!=null&&Number.isFinite(Number(g.gamma_flip)))parts.push(`Gamma Flip $${Number(g.gamma_flip).toFixed(2)}`);
  if(g.net_gex!=null&&Number.isFinite(Number(g.net_gex))&&Math.abs(Number(g.net_gex))>1)parts.push(`Net ${(Number(g.net_gex)/1000000).toFixed(1)}M`);
  let metrics=parts.length?`<small>${parts.join(" · ")}</small>`:"";
  return `<div class="gex ${tone}"><b>${title}</b>${metrics}<small>${esc(g.interpretation_cn||g.limitations||"用于识别短约波动放大/钉住风险。")}</small></div>`;
}
function stockPanicTone(p){let s=Number(p?.stock_panic_score||0);if(s>=75)return"bad";if(s>=45)return"wait";return"good"}
function driverText(drivers){const map={high_realized_vol:"高实现波动",volatility_acceleration:"波动加速",drawdown:"回撤压力",high_beta:"高Beta",volume_spike:"放量",gex_risk:"GEX风险",event_risk:"事件风险",baseline_market_transmission:"市场传导"};return (drivers||[]).map(x=>map[x]||x).join(" / ")||"--"}
function stockPanicHtml(p,compact=false){
  if(!p||!p.available)return compact?'<span class="tag wait">个股VIX --</span>':'<div class="gex wait"><small>个股 VIX Proxy 暂无足够行情数据。</small></div>';
  let tone=stockPanicTone(p),v=p.stock_vix_equivalent==null?"--":Number(p.stock_vix_equivalent).toFixed(1),s=p.stock_panic_score==null?"--":Number(p.stock_panic_score).toFixed(1);
  if(compact)return `<span class="tag ${tone}">个股VIX ${v} / ${s}分</span>`;
  return `<div class="gex ${tone}"><b>个股 VIX ${v} · 恐慌分 ${s}</b><small>${esc(p.panic_level_cn||"--")} · ${esc(driverText(p.drivers))}</small><small>HV5 ${p.hv5==null?"--":Number(p.hv5).toFixed(1)+"%"} · HV20 ${p.hv20==null?"--":Number(p.hv20).toFixed(1)+"%"} · 回撤 ${pct(p.drawdown_60d)} · Beta ${p.beta_spy_qqq==null?"--":Number(p.beta_spy_qqq).toFixed(2)}</small></div>`;
}
function dataSourcesHtml(s){
  if(!s||!Object.keys(s).length)return "";
  const parts=[];
  if(s.history)parts.push(`H:${s.history}`);
  if(s.options)parts.push(`Exp:${s.options}`);
  if(s.option_chain)parts.push(`Chain:${s.option_chain}`);
  if(s.fundamentals)parts.push(`F:${s.fundamentals}`);
  return parts.length?`<small>Data ${esc(parts.join(" / "))}</small>`:"";
}
function macroTone(m){let mode=String(m?.mode||"");if(m?.systemic_crisis||mode==="crisis_guard")return"bad";if(mode==="panic_reclaim"||mode==="panic_watch"||mode==="defensive")return"wait";return"good"}
function macroCard(title,value,tone){return `<div class="macro-card ${tone}"><span>${title}</span><b>${value}</b></div>`}
function renderMacro(m,p){
  const exposure=p&&p.available?`${Number(p.max_gross_exposure_pct||0).toFixed(0)}%`:"--";
  const exposureTone=Number(p?.gross_exposure_multiplier??1)>=0.75?"good":Number(p?.gross_exposure_multiplier??1)>=0.5?"wait":"bad";
  if(!m||!m.available){$("macroBanner").innerHTML=macroCard("\u5b8f\u89c2\u73af\u5883","VIX --","wait")+macroCard("\u6a21\u5f0f","\u5f85\u66f4\u65b0","wait")+macroCard("\u6050\u614c\u5206","--","wait")+macroCard("\u5371\u673a\u8fc7\u6ee4","\u672a\u77e5","wait")+macroCard("\u5efa\u8bae\u603b\u655e\u53e3",exposure,exposureTone);return}
  let tone=macroTone(m),name=m.display_name||"VIX",val=m.value==null?"--":Number(m.value).toFixed(2);
  $("macroBanner").innerHTML=[
    macroCard("\u5b8f\u89c2\u73af\u5883",`${esc(name)} ${val}`,tone),
    macroCard("\u72b6\u6001",esc(m.label_cn||"--"),tone),
    macroCard("\u6a21\u5f0f",esc(m.mode_cn||"--"),tone),
    macroCard("\u5371\u673a\u8fc7\u6ee4",m.systemic_crisis?"\u5df2\u89e6\u53d1":"\u672a\u89e6\u53d1",m.systemic_crisis?"bad":tone),
    macroCard("\u5efa\u8bae\u603b\u655e\u53e3",`${exposure} · ${esc(p?.regime_cn||"--")}`,exposureTone),
  ].join("");
}
async function readJson(res){
  const text=await res.text();
  let data=null;
  try{data=text?JSON.parse(text):{}}
  catch(e){throw new Error(text||`HTTP ${res.status}`)}
  if(!res.ok)throw new Error(data.detail||data.message||`HTTP ${res.status}`);
  return data;
}
function universeOptions(items){
  let groups=[];(items||[]).forEach(x=>{let key=x.group_label||"其他股票池",g=groups.find(y=>y.key===key);if(!g){g={key,items:[]};groups.push(g)}g.items.push(x)});
  return groups.map(g=>`<optgroup label="${esc(g.key)}">${g.items.map(x=>{let w=x.recent_win||{},score=Number(w.recent_win_score||0);let meta=w.candidate_count?`${winText(w)} · 候选 ${w.candidate_count} · GEX ${pct(w.gex_coverage)}`:"尚无近期候选";return `<option value="${esc(x.universe)}" data-score="${score}">${esc(x.label)} · ${meta} · ${esc(x.tier)} · 成本${esc(x.run_cost||"中")}</option>`}).join("")}</optgroup>`).join("");
}
function sortMark(key){return sortState.key===key?`<span class="sort-mark">${sortState.dir>0?"ASC":"DESC"}</span>`:""}
function headerCell(key,label){return `<th class="sortable" data-sort="${key}">${label}${sortMark(key)}</th>`}
function updateHeader(poolType){
  $("head").innerHTML = `<tr>${headerCell("pe","\u6807\u7684")}${headerCell("trend","\u8d70\u52bf")}${headerCell("gex","\u4e13\u9879\u8bc1\u636e + GEX")}${headerCell("tunnel","\u65e5\u96a7\u9053\u534f\u5546")}${headerCell("stake","\u68c0\u9a8c\u7b79\u7801")}${headerCell("unified","\u7edf\u4e00\u8bc1\u636e")}${headerCell("status","\u4e09\u5c42\u72b6\u6001 + GEX")}${headerCell("invalidation","\u5931\u6548\u9000\u51fa")}${headerCell("advice","\u7814\u7a76\u5efa\u8bae")}</tr>`;
  document.querySelectorAll("th.sortable").forEach(th=>th.onclick=()=>applySort(th.dataset.sort));
}
function mainSignalHtml(poolType,o,p,pe2){
  if(poolType==="pre"){
    if(pe2&&pe2.report){let dd=pe2.report.trading_days_to_report??"--",iw=Boolean(pe2.in_target_window),sc=pe2.score?.final;return `<div class="stack"><b class="score">${iw?"目标窗口":"远期观察"}</b><small>${esc(pe2.report.target_date||"--")} · ${esc(dd)} 个交易日</small><small>预期修正评分 ${pct(sc)}</small></div>`}
    return `<div class="stack"><b>无预期窗口</b><small>等待财报日期与预期修正证据</small></div>`;
  }
  if(poolType==="peer"){
    if(p){return `<div class="stack relay"><b>${esc(p.leader_symbol)} 先发</b><small>配对 ${pct(p.peer_similarity)}</small><small>相对滞涨 ${pct(p.relative_lag_gap)}</small></div>`}
    return `<div class="stack"><b>无接力</b><small>没有通过子赛道、规模方向与风格筛选</small></div>`;
  }
  if(o&&Object.keys(o).length){return `<div class="stack"><b>${esc(o.signal||"观察")}</b><small>机会分 ${Number(o.opportunity_score||0).toFixed(1)}</small><small>${esc(o.candidate_label||o.research_tier||o.opportunity_tier||"常规候选")}</small></div>`}
  return `<div class="stack"><b>未入池</b><small>第一层暂无机会质量记录</small></div>`;
}
function supportEvidenceHtml(poolType,o,p,pe2){
  let gex=gexHtml(o.gamma_exposure);
  let panic=stockPanicHtml(o.stock_panic);
  if(p){let dateNote=p.target_report_date_requires_manual_check?"披露日待人工确认":"披露日 "+(p.target_report_date||"--");let capRatio=p.leader_to_target_market_cap_ratio==null?"--":Number(p.leader_to_target_market_cap_ratio).toFixed(2)+"x";return `<div class="stack relay"><b>${esc(p.leader_symbol)} · ${esc(p.group_label||"同行")}</b><small>${esc(p.sublane_relation_display||p.sublane_relation||"同行传导")}</small><small>${esc(dateNote)} · ${esc(p.transmission_direction||"传导方向待确认")}</small><small>龙头/标的市值 ${esc(capRatio)} · 同行 ${cap(p.leader_market_cap)} · PE ${pe(p.leader_trailing_pe)}</small><small>财报前 ${money(p.leader_pre_report_price)} → 现价 ${money(p.leader_latest_price)} · ${pct(p.leader_post_report_return)}</small><small>标的 ${money(p.target_start_price)} → ${money(p.target_latest_price)} · ${pct(p.target_return)}</small><small>模型预期补涨 ${pct(p.expected_catch_up_return)} → ${money(p.expected_target_price)}</small>${panic}${gex}</div>`}
  if(poolType==="pre"&&pe2&&pe2.report){return `<div class="stack"><b>财报窗口</b><small>目标日 ${esc(pe2.report.target_date||"--")}</small><small>退出：财报前 2 个交易日复核/离场</small><small>证据：预期修正 + 启动择时 + 日隧道协商</small>${panic}${gex}</div>`}
  if(o&&Object.keys(o).length){return `<div class="stack"><b>实股买入信号</b><small>来源池 ${(o.source_pools||[]).map(esc).join(" / ")||"--"}</small><small>观察区 ${money(o.entry_zone_low)} - ${money(o.entry_zone_high)}</small><small>风险 ${esc(o.risk_level||"--")} · 流动性 ${Number(o.liquidity_score||0).toFixed(2)}</small>${panic}${gex}</div>`}
  return `<div class="stack"><b>暂无专项证据</b><small>等待第一层、第二层或同行接力补充</small>${panic}${gex}</div>`;
}
function researchAdviceHtml(c,o,t,u,r,h,gex,grade,decision){
  const confirm=String(c.pullback_confirmation_status||"");
  const reject=String(c.pullback_rejection_status||"");
  const prob=Number(r.probability||0);
  const exec=Number(r.win_sort?.execution_score||0);
  const gexRisk=String(gex?.risk_level||"").toLowerCase();
  let action="继续观察";
  if(confirm==="STRONG_CONFIRMED") action="优先跟踪";
  else if(confirm==="CONFIRMED") action="跟踪";
  else if(confirm==="WEAK_CONFIRMED") action="等待回踩";
  else if(reject==="STRONG_WATCH"||reject==="WATCH") action="等待突破确认";
  if(prob>0&&prob<0.46) action="仅纸面观察";
  if(gexRisk==="high"&&action!=="仅纸面观察") action=action+"，降级风控";
  const lines=[];
  if(decision) lines.push(decision);
  if(confirm==="STRONG_CONFIRMED") lines.push("回调结束已确认，重点看能否守住突破位。");
  else if(confirm==="CONFIRMED") lines.push("已有突破确认，继续复核收盘延续性。");
  else if(reject==="STRONG_WATCH"||reject==="WATCH") lines.push("已有回调承接，等待突破价触发二次确认。");
  else lines.push("缺少明确回调承接或启动确认，降低优先级。");
  if(h.invalidation_price) lines.push(`失效位 ${money(h.invalidation_price)}。`);
  if(c.waiting_breakout_price) lines.push(`等待突破 ${money(c.waiting_breakout_price)}。`);
  if(gexRisk==="high") lines.push("GEX 偏高，短线波动可能放大。");
  if(exec===0) lines.push("执行分为 0，暂不做试仓测算。");
  return `<div class="stack"><b>${esc(action)}</b><small>${esc(grade||"研究观察")}</small><small>${esc(lines.slice(0,3).join(" "))}</small></div>`;
}

let currentRows=[];
let sortState={key:"overall",dir:-1};
function num(v,def=0){let n=Number(v);return Number.isFinite(n)?n:def}
function rowCtx(c){
  const compact=!c.opportunity&&Object.prototype.hasOwnProperty.call(c,"current_price");
  const o=compact?{spot:c.current_price,market_cap:c.market_cap,trailing_pe:c.pe,opportunity_score:c.stock_signal_score,trend_30d:c.trend_30d,recent_closes:c.recent_closes||[],source_pools:c.source_pools||[],entry_zone_low:c.entry_zone_low,entry_zone_high:c.entry_zone_high,risk_level:c.risk_level,liquidity_score:c.liquidity_score,candidate_label:c.candidate_label,gamma_exposure:c.gamma_exposure||{available:Boolean(c.gex_level),risk_level:c.gex_level,regime:c.gex_regime},stock_panic:c.stock_panic||{available:Boolean(c.stock_vix_equivalent),stock_vix_equivalent:c.stock_vix_equivalent,stock_panic_score:c.stock_panic_score,panic_level:c.stock_panic_level,panic_level_cn:c.stock_panic_level_cn,drivers:c.stock_panic_drivers,hv5:c.stock_panic_hv5,hv20:c.stock_panic_hv20,beta_spy_qqq:c.stock_panic_beta,drawdown_60d:c.stock_panic_drawdown_60d,volume_spike:c.stock_panic_volume_spike},risk_flags:c.risk_flags||[]}:c.opportunity||{};
  const t=compact?{signal_cn:c.launch_signal_status,launch_score:c.launch_signal_score,daily_tunnel:c.daily_tunnel||{score:c.daily_tunnel_score,label:c.daily_tunnel_label}}:c.timing||{};
  const u=t.daily_tunnel||{},p=c.peer_earnings,e=compact?{signal_cn:c.event_radar_status}:c.event||{},x=compact?{opportunity:Boolean(c.stock_signal_score),timing:Boolean(c.launch_signal_status),event_clear:c.event_radar_status!=="??",peer_earnings:false}:c.dimensions||{};
  const h=compact?{invalidation_price:c.invalidation_price,test_shares:c.test_shares,requested_qty:c.requested_qty,allowed_qty:c.allowed_qty,capital_required:c.capital_required,max_test_loss:c.max_test_loss,sizing_mode:c.sizing_mode,eligible:false,reason:c.short_reason,open_risk_status:c.open_risk_status,open_risk_reason:c.open_risk_reason,trade_risk_check:c.trade_risk_check,target_price:c.target_price,reward_risk_ratio:c.reward_risk_ratio,stop_loss_pct:c.stop_loss_pct,exit_action:c.exit_action,exit_reason:c.exit_reason,exit_risk_check:c.exit_risk_check}:c.hypothesis_test||{};
  const r=compact?{probability:c.unified_probability??Number(c.unified_score||0)/100,score:c.unified_score,probability_low:c.unified_probability_low,probability_high:c.unified_probability_high,win_sort:c.win_sort||{historical_win_rate:c.historical_win_rate},components:c.evidence_components||[],note:c.evidence_note}:c.research_evidence||{};
  return {compact,o,t,u,p,e,x,h,r,gex:o.gamma_exposure||{}};
}
function gexSortScore(g){
  if(!g||!g.available)return -1;
  let regime=String(g.regime||"").toLowerCase(),risk=String(g.risk_level||"").toLowerCase(),score=0;
  if(regime==="positive")score+=4; else if(regime==="neutral")score+=2; else if(regime==="negative")score+=1; else if(regime==="negative_high")score-=2;
  if(risk==="low")score+=3; else if(risk==="medium")score+=1; else if(risk==="high")score-=3;
  score+=Math.max(-1,Math.min(1,num(g.net_gex,0)/1000000))*0.2;
  return score;
}
function statusGreenCount(c,ctx){let x=ctx.x||{},g=ctx.gex||{};return [x.opportunity,c.pullback_rejection_status&&c.pullback_rejection_status!=="NONE",c.pullback_confirmation_status&&c.pullback_confirmation_status!=="NONE",x.timing,Boolean(ctx.u.sample_days),x.peer_earnings,x.event_clear,g.available&&String(g.risk_level||"").toLowerCase()==="low"].filter(Boolean).length}
function advicePriority(c,ctx){let confirm=String(c.pullback_confirmation_status||""),reject=String(c.pullback_rejection_status||""),prob=num(ctx.r.probability,0);if(confirm==="STRONG_CONFIRMED")return 6;if(confirm==="CONFIRMED")return 5;if(confirm==="WEAK_CONFIRMED")return 3;if(reject==="STRONG_WATCH")return 4;if(reject==="WATCH")return 3;if(prob>=0.55)return 2;if(prob>0)return 1;return 0}
function sortValue(c,key){let ctx=rowCtx(c),o=ctx.o,u=ctx.u,h=ctx.h,r=ctx.r,g=ctx.gex,p=o.stock_panic||{};switch(key){case "pe":{let pe=num(o.trailing_pe,Number.POSITIVE_INFINITY);return Number.isFinite(pe)&&pe>0?-pe:-999999}case "trend":return num(o.trend_30d,-999);case "opportunity":return num(o.opportunity_score,-1);case "gex":return num(p.stock_panic_score,-1)*0.7+gexSortScore(g)*8;case "tunnel":return num(u.score,-1);case "stake":return num(h.test_shares,0)||num(h.capital_required,0);case "unified":return num(r.probability,0);case "status":return statusGreenCount(c,ctx);case "invalidation":return num(c.invalidation_buffer_pct, h.invalidation_price&&o.spot?((num(o.spot)-num(h.invalidation_price))/num(o.spot)): -9);case "advice":return advicePriority(c,ctx);case "overall":default:return num(c.overall_sort_score, num(r.win_sort?.sort_score,0));}}
function sortedRows(rows){let arr=[...(rows||[])];arr.sort((a,b)=>{let av=sortValue(a,sortState.key),bv=sortValue(b,sortState.key);if(av!==bv)return sortState.dir*(av-bv);return String(a.symbol||"").localeCompare(String(b.symbol||""));});return arr}
function bestPickScore(c){
  let ctx=rowCtx(c),o=ctx.o,u=ctx.u,h=ctx.h,r=ctx.r,g=ctx.gex;
  let score=num(c.overall_sort_score,num(r.win_sort?.sort_score,0))*100;
  score+=num(r.probability,0.5)*18;
  score+=statusGreenCount(c,ctx)*3;
  score+=Math.max(0,Math.min(100,num(u.score,0)))*0.08;
  score+=Math.max(0,Math.min(100,num(o.opportunity_score,0)))*0.05;
  if(String(h.open_risk_status||"")==="ALLOW_OPEN")score+=8;
  else if(String(h.open_risk_status||"")==="REDUCE_QTY")score+=4;
  else if(String(h.open_risk_status||"")==="BLOCK_OPEN")score-=18;
  if(String(h.exit_action||"")==="EXIT_ALL")score-=35;
  let risk=String(g?.risk_level||"").toLowerCase(),regime=String(g?.regime||"").toLowerCase();
  if(risk==="high"||regime==="negative_high")score-=8;
  else if(risk==="low")score+=3;
  return score;
}
function isBlockedPick(c){let h=rowCtx(c).h||{};return String(h.open_risk_status||"")==="BLOCK_OPEN"||String(h.exit_action||"")==="EXIT_ALL"}
function selectBestPick(rows){let arr=[...(rows||[])];if(!arr.length)return null;let tradable=arr.filter(c=>!isBlockedPick(c));let pool=tradable.length?tradable:arr;pool.sort((a,b)=>bestPickScore(b)-bestPickScore(a)||String(a.symbol||"").localeCompare(String(b.symbol||"")));return pool[0]}
function sameSymbol(a,b){return String(a?.symbol||"")===String(b?.symbol||"")&&String(a?.symbol||"")!==""}
function renderBestPick(best){
  if(!best){$("bestPick").classList.remove("show");$("bestPick").innerHTML="";return}
  let ctx=rowCtx(best),o=ctx.o,u=ctx.u,h=ctx.h,r=ctx.r,g=ctx.gex,prob=r.probability,score=bestPickScore(best);
  let riskNote=h.open_risk_status==="BLOCK_OPEN"?"当前风控禁止开仓，仅适合人工复核":h.open_risk_status==="REDUCE_QTY"?`风控建议降仓至 ${h.allowed_qty??"--"} 股`:"风控未阻断";
  let gexNote=g&&g.available?`GEX ${String(g.risk_level||"--").toUpperCase()} / ${esc(g.regime||"--")}`:"GEX --";
  $("bestPick").innerHTML=`<h2>今晚优先复核标的：<b>${esc(best.symbol)}</b></h2><div class="line"><span>现价 <b>${money(o.spot)}</b></span><span>统一概率 <b>${pct(prob)}</b></span><span>综合分 <b>${score.toFixed(1)}</b></span><span>日隧道 <b>${u.score==null?"--":Number(u.score).toFixed(1)}</b></span><span>${esc(gexNote)}</span><span>${esc(h.open_risk_status||"--")}</span></div><small>${esc(riskNote)}；该标的是当前候选中综合排序最高的一只，只作为研究优先级，不代表确定性买入建议。</small>`;
  const llm=best.llm_review||{};
  if(llm.available){
    const focus=llm.review_focus==="peer_relay"?"同行财报接力复核":llm.review_focus==="ai_supply_chain"?"AI关键供应链复核":"三层信号复核";
    $("bestPick").insertAdjacentHTML("beforeend",`<div class="llm-review"><b>DeepSeek ${esc(focus)}：${esc(llm.verdict||"已复核")} · ${esc(llm.overnight_action||"人工复核")}</b><small>${esc(llm.summary||"")}</small><small>专项：${esc((llm.specialist_notes||[]).slice(0,2).join("；")||"--")}</small><small>支持：${esc((llm.supporting_points||[]).slice(0,2).join("；")||"--")}</small><small>反对/风险：${esc((llm.objections||llm.risk_flags||[]).slice(0,2).join("；")||"--")}</small></div>`);
  }else{
    $("bestPick").insertAdjacentHTML("beforeend",`<div class="llm-review muted"><b>DeepSeek 决策辅助：未复核</b><small>运行三层分析后，会对 Top 候选生成复核意见。</small></div>`);
  }
  $("bestPick").classList.add("show");
}
function applySort(key){if(!key)return;if(sortState.key===key)sortState.dir*=-1;else{sortState.key=key;sortState.dir=(key==="pe"?1:-1)}updateHeader(currentPoolType||getPoolType($("universe").value));renderRows(sortedRows(currentRows));}
let currentPoolType="std";
function render(d){
  currentPoolType=getPoolType($("universe").value);
  updateHeader(currentPoolType);
  renderMacro(d.macro_regime,d.portfolio_timing);
  currentRows=d.rows||[];
  if(d.snapshot){
    let shown=currentRows.length,total=d.snapshot.total_candidate_count||d.snapshot.candidate_count||shown;
    let note=$("poolNote").textContent.replace(new RegExp(" · display [0-9]+/[0-9]+"),"");
    $("poolNote").textContent=`${note} · display ${shown}/${total}`;
  }
  renderRows(sortedRows(currentRows));
  const best=selectBestPick(sortedRows(currentRows)||[]);
  if(best&&$("poolNote")){
    $("poolNote").textContent=$("poolNote").textContent.replace(/ · (报告Top|Top) .*/, "")+` · 当前Top ${best.symbol||"--"}`;
  }
}
function renderRows(rows){
  const best=selectBestPick(rows||[]);
  renderBestPick(best);
  const ordered=best?[best,...(rows||[]).filter(c=>!sameSymbol(c,best))]:(rows||[]);
  $("rows").innerHTML=ordered.map(c=>{
    const compact=!c.opportunity&&Object.prototype.hasOwnProperty.call(c,"current_price");
    const o=compact?{spot:c.current_price,market_cap:c.market_cap,trailing_pe:c.pe,opportunity_score:c.stock_signal_score,trend_30d:c.trend_30d,recent_closes:c.recent_closes||[],source_pools:c.source_pools||[],entry_zone_low:c.entry_zone_low,entry_zone_high:c.entry_zone_high,risk_level:c.risk_level,liquidity_score:c.liquidity_score,candidate_label:c.candidate_label,gamma_exposure:c.gamma_exposure||{available:Boolean(c.gex_level),risk_level:c.gex_level,regime:c.gex_regime},stock_panic:c.stock_panic||{available:Boolean(c.stock_vix_equivalent),stock_vix_equivalent:c.stock_vix_equivalent,stock_panic_score:c.stock_panic_score,panic_level:c.stock_panic_level,panic_level_cn:c.stock_panic_level_cn,drivers:c.stock_panic_drivers,hv5:c.stock_panic_hv5,hv20:c.stock_panic_hv20,beta_spy_qqq:c.stock_panic_beta,drawdown_60d:c.stock_panic_drawdown_60d,volume_spike:c.stock_panic_volume_spike},risk_flags:c.risk_flags||[]}:c.opportunity||{};
    const t=compact?{signal_cn:c.launch_signal_status,launch_score:c.launch_signal_score,daily_tunnel:c.daily_tunnel||{score:c.daily_tunnel_score,label:c.daily_tunnel_label}}:c.timing||{};
    const u=t.daily_tunnel||{},p=c.peer_earnings,e=compact?{signal_cn:c.event_radar_status}:c.event||{},x=compact?{opportunity:Boolean(c.stock_signal_score),timing:Boolean(c.launch_signal_status),event_clear:c.event_radar_status!=="回避",peer_earnings:false}:c.dimensions||{};
    const h=compact?{invalidation_price:c.invalidation_price,test_shares:c.test_shares,requested_qty:c.requested_qty,allowed_qty:c.allowed_qty,capital_required:c.capital_required,max_test_loss:c.max_test_loss,sizing_mode:c.sizing_mode,eligible:Boolean(c.test_shares),reason:c.short_reason,open_risk_status:c.open_risk_status,open_risk_reason:c.open_risk_reason,trade_risk_check:c.trade_risk_check,target_price:c.target_price,reward_risk_ratio:c.reward_risk_ratio,stop_loss_pct:c.stop_loss_pct,exit_action:c.exit_action,exit_reason:c.exit_reason,exit_risk_check:c.exit_risk_check}:c.hypothesis_test||{};
    const r=compact?{probability:c.unified_probability??Number(c.unified_score||0)/100,score:c.unified_score,probability_low:c.unified_probability_low,probability_high:c.unified_probability_high,win_sort:c.win_sort||{historical_win_rate:c.historical_win_rate},components:c.evidence_components||[],note:c.evidence_note}:c.research_evidence||{},pe2=c.pre_earnings||{};
    const macro=c.macro_regime||{};
    const targetPrice=p?.target_latest_price??o.spot, targetCap=p?.target_market_cap??o.market_cap, targetPe=p?.target_trailing_pe??o.trailing_pe??o.forward_pe;
    const gex=o.gamma_exposure||{},gexChip=gexHtml(gex,true),components=(r.components||[]).map(x=>x.label).join(" + ")||"证据不足";
    const grade=c.overall_status||c.decision_grade||"研究观察", decision=c.short_reason||c.decision||"";
    const hlCls=(pe2&&pe2.in_target_window)?' class="highlight"':'';
    return `<tr${hlCls}>
      <td><div class="stack"><b>${esc(c.symbol)}</b><small>${money(targetPrice)}</small><small>市值 ${cap(targetCap)}<br>PE ${pe(targetPe)}</small>${gexChip}</div></td>
      <td>${trendCell(o)}</td>
      <td>${supportEvidenceHtml(currentPoolType,o,p,pe2)}</td>
      <td><div class="stack"><b class="score">${u.score==null?"--":Number(u.score).toFixed(1)+" 分"}</b><small>${esc(u.label||"尚未计算")}</small><small>${esc(u.current_zone||"--")}</small><small>拐点 ${pct(u.turning_point_score)} · 当前上行 ${esc(u.current_up_cycle_days??0)} 日</small><small>最长上行 ${esc(u.longest_up_cycle_days??0)} 日 · 上行力量 ${pct(u.up_cycle_power)}</small></div></td>
      <td><div class="stack">${h.eligible?`<b>${esc(h.test_shares)} 股 · ${esc(h.sizing_mode==="micro_probe"?"微型试仓":h.sizing_mode==="risk_reduced"?"风控降仓":"研究试仓")}</b><small>占用 $${Number(h.capital_required||0).toFixed(0)} · 最大检验风险 $${Number(h.max_test_loss||0).toFixed(0)}</small><small>${esc(h.open_risk_status||"--")} · ${esc(h.open_risk_reason||h.reason||"")}</small>`:`<b>${esc(h.sizing_mode==="risk_blocked"?"风控禁止开仓":h.sizing_mode==="paper_track"?"纸面跟踪":"暂不试仓")}</b><small>${esc(h.open_risk_status||"--")} · ${esc(h.open_risk_reason||h.reason||"等待证据")}</small>`}</div></td>
      <td><div class="stack"><b class="score">${pct(r.probability)}</b><small>区间 ${pct(r.probability_low)} - ${pct(r.probability_high)}</small><small>胜算分 ${pct(r.win_sort?.sort_score)} · 历史 ${pct(r.win_sort?.historical_win_rate)}</small><small>${esc(r.win_sort?.historical_source||"历史代理")} · 风报比 ${Number(r.win_sort?.risk_reward||0).toFixed(2)} · 执行 ${pct(r.win_sort?.execution_score)}</small><small>${esc(components)}</small></div></td>
      <td><div class="stack">${dot(x.opportunity,`机会 ${Number(o.opportunity_score||0).toFixed(1)}`)}${dot(c.pullback_rejection_status&&c.pullback_rejection_status!=="NONE",`回调承接 ${c.pullback_rejection_status||"--"}`)}${dot(c.pullback_confirmation_status&&c.pullback_confirmation_status!=="NONE",`启动确认 ${c.pullback_confirmation_status||"--"}`)}${dot(x.timing,t.signal_cn||"等待启动")}${dot(Boolean(u.sample_days),u.label||"日线待算")}${dot(x.peer_earnings,p?"同行接力":"无接力")}${dot(x.event_clear,e.signal_cn||e.signal||"暂无告警")}${dot(gex.available,gex.available?`GEX ${String(gex.risk_level||"--").toUpperCase()}`:"GEX 无数据")}${dot((o.stock_panic||{}).available,`个股VIX ${(o.stock_panic||{}).stock_vix_equivalent??"--"}`)}${dot(macro.available && !macro.systemic_crisis,macro.available?`市场VIX ${macro.label_cn||"--"}`:"市场VIX --")}</div></td>
      <td><div class="stack"><b>${h.invalidation_price?esc("$"+Number(h.invalidation_price).toFixed(2)):"--"}</b><small>${esc(h.exit_action||"HOLD")} · ${esc(h.exit_reason||"尚未触发退出条件")}</small><small>等待突破 ${c.waiting_breakout_price?money(c.waiting_breakout_price):"--"} · 目标 ${h.target_price?money(h.target_price):"--"}</small><small>${esc(h.exit_rule||"--")}</small></div></td>
      <td class="muted">${researchAdviceHtml(c,o,t,u,r,h,gex,grade,decision)}</td>
    </tr>`;
  }).join("")||`<tr><td colspan="9" class="empty">该股票池尚无机会候选，请先在“实股信号”中更新股票池。</td></tr>`;
}
function detailHtml(d){
  const o=d.opportunity||{},t=d.timing||{},u=t.daily_tunnel||{},e=d.event||{},r=d.research_evidence||{},h=d.hypothesis_test||{},pr=o.pullback_rejection||{},pc=o.pullback_confirmation||{},g=o.gamma_exposure||{};
  return `<h2>${esc(d.symbol)} · 懒加载详情</h2><div class="detail-grid">
    <div class="kv"><span>统一概率</span><b>${pct(r.probability)}</b></div>
    <div class="kv"><span>机会分</span><b>${Number(o.opportunity_score||0).toFixed(1)}</b></div>
    <div class="kv"><span>启动状态</span><b>${esc(t.signal_cn||t.signal||"--")}</b></div>
    <div class="kv"><span>事件状态</span><b>${esc(e.signal_cn||e.signal||"--")}</b></div>
    <div class="kv"><span>回调承接</span><b>${esc(pr.signal_stage||"--")}</b></div>
    <div class="kv"><span>回调确认</span><b>${esc(pc.signal_stage||"--")}</b></div>
    <div class="kv"><span>等待突破</span><b>${money(pc.waiting_breakout_price||pr.waiting_breakout_price)}</b></div>
    <div class="kv"><span>失效价</span><b>${money(pc.invalidation_price||pr.invalidation_price||h.invalidation_price)}</b></div>
  </div><div class="detail-section"><b>第一层 · 回调承接</b><div class="muted">${esc(pr.human_readable_reason||"当前没有回调承接信号。")} · 下影线占比 ${pct(pr.lower_shadow_pct_of_range)} · 收盘位置 ${pct(pr.close_location)} · 最近支撑 ${money(pr.nearest_support)}</div></div>
  <div class="detail-section"><b>第二层 · 回调结束确认</b><div class="muted">${esc(pc.human_readable_reason||"等待后续确认。")} · 确认日期 ${esc(pc.confirmation_date||"--")} · 类型 ${esc(pc.confirmation_type||"--")}</div></div>
  <div class="detail-section"><b>第三层 · 事件与 GEX 风险</b><div class="muted">${esc(e.reason||e.top_event||"暂无事件告警。")}</div>${gexHtml(g)}</div>
  <div class="detail-section"><b>日隧道协商</b><div class="muted">${esc(u.label||"--")} · ${esc(u.current_zone||"--")} · 分数 ${u.score==null?"--":Number(u.score).toFixed(1)}</div></div>
  <div class="detail-section"><b>统一证据</b><div class="muted">${esc((r.components||[]).map(x=>x.label).join(" + ")||"证据不足")} · ${esc(d.decision||"仅供研究复核，不构成确定性买入建议。")}</div></div>`;
}
async function fetchDetail(symbol,url){
  $("detailPanel").classList.add("show");$("detailPanel").innerHTML=`<div class="empty">正在加载 ${esc(symbol)} 详情...</div>`;
  let d=await readJson(await fetch(url));$("detailPanel").innerHTML=detailHtml(d);
}
function updatePoolNote(items){
  let selected=(items||[]).find(x=>x.universe===$("universe").value),w=selected?.recent_win||{};
  $("poolNote").textContent=selected?`当前池排序 #${selected.dynamic_rank||"--"}：${selected.label} · ${winText(w)} · 历史 ${pct(w.historical_win_rate)} · 统一概率 ${pct(w.unified_probability)} · GEX覆盖 ${pct(w.gex_coverage)} · 候选 ${w.candidate_count||0}`:"";
}
let universeCatalog=[];
async function loadUniverses(preferredUniverse=""){
  const previous=preferredUniverse||$("universe")?.value||"";
  let d=await readJson(await fetch("/research-universes"));universeCatalog=d.universes||[];
  $("universe").innerHTML=universeOptions(universeCatalog);
  if(previous&&universeCatalog.some(x=>x.universe===previous)){$("universe").value=previous}else{let top=universeCatalog.find(x=>x.default_selected)||universeCatalog[0];if(top)$("universe").value=top.universe}
  updatePoolNote(universeCatalog);
}
async function load(){updatePoolNote(universeCatalog);let lim=$("rowLimit")?.value||"20";let d=await readJson(await fetch(`/research-signal-hub?universe=${encodeURIComponent($("universe").value)}&limit=${encodeURIComponent(lim)}&compact=true`));render(d)}
function showProgress(d){$("progress").classList.add("show");$("phase").textContent=d.message||"正在运行...";$("pct").textContent=`${Math.round(Number(d.progress||0)*100)}%`;$("bar").style.width=`${Math.round(Number(d.progress||0)*100)}%`;if(d.result){let g=d.result.decision_grades||{},fresh=`行情${d.result.market_evidence_refreshed?"已更新":"复用缓存"} · 公告/新闻${d.result.event_evidence_refreshed?"已更新":"复用缓存"}`;$("summary").textContent=`${fresh} · 机会候选 ${d.result.candidate_count||0} 只 · 启动信号 ${d.result.timing_signal_count||0} 只 · 同行接力 ${d.result.peer_earnings_signal_count||0} 只 · 决策卡片 ${d.result.decision_count||0} 张 · ${Object.entries(g).map(([k,v])=>`${k} ${v}`).join(" / ")}`}}
async function poll(jobId,softErrors=0,requestedUniverse=""){const keepUniverse=requestedUniverse||$("universe").value;try{let d=await readJson(await fetch(`/research-signal-hub/run/${encodeURIComponent(jobId)}`));showProgress(d);if(d.status==="completed"){$("run").disabled=false;await loadUniverses(keepUniverse);await load();return}if(d.status==="failed"){$("run").disabled=false;throw new Error(d.message||"三层分析失败")}setTimeout(()=>poll(jobId,0,keepUniverse),1500)}catch(e){if(softErrors<20){$("progress").classList.add("show");$("phase").textContent="结果同步中...";$("summary").textContent="后台任务仍在运行，刚才有一次临时同步失败，正在自动重试。";setTimeout(()=>poll(jobId,softErrors+1,keepUniverse),1800);return}showError(e)}}
function showError(e){$("run").disabled=false;$("progress").classList.add("show");$("phase").textContent=e.message||"运行失败";$("pct").textContent="";$("bar").style.width="100%"}
async function run(){let universe=$("universe").value;$("run").disabled=true;$("progress").classList.add("show");$("phase").textContent="三维研究 Agent 正在启动...";$("summary").textContent="超过 24 小时的行情与公告/新闻事件会自动更新；仍在有效期内的证据将复用缓存。";try{let d=await readJson(await fetch("/research-signal-hub/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({universe,refresh_evidence:true,top:100})}));poll(d.job_id,0,universe)}catch(e){showError(e)}}
async function launchChain(symbol){$("progress").classList.add("show");$("phase").textContent=`正在启动 ${symbol} 五层研究链...`;$("summary").textContent="将按拆链、初筛、深审、交叉验证、策略测算五层生成可审计研究链。";let universe=$("universe").value;let d=await readJson(await fetch("/research-chain/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({symbol,universe,market:"US equities",goal:`从三维量化信号总览进入五层研究链。来源股票池：${universe}。请结合当前实股信号、启动信号、事件雷达、Gamma Exposure 与同行接力证据进行复核。`})}));$("phase").textContent=`五层研究链已启动：${symbol}`;$("summary").innerHTML=`Swarm Run: <a href="/swarm/runs/${encodeURIComponent(d.id)}" target="_top">${esc(d.id)}</a>。可在 Swarm 运行页查看各层进度。`;}
$("universe").onchange=()=>load().catch(e=>$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`);
$("rowLimit").onchange=()=>load().catch(e=>$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`);
$("load").onclick=()=>load().catch(e=>$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`);
$("run").onclick=()=>run().catch(showError);
loadUniverses().then(load).catch(e=>$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`);
</script>
</body>
</html>
"""
