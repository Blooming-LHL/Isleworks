(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const text = (el, value) => { el.textContent = value === undefined || value === null ? '—' : String(value); };
  const fish = value => { const n = Number(value); return Number.isFinite(n) ? `${(n / 1000).toFixed(3).replace(/\.000$/, '').replace(/(\.\d)00$/, '$1').replace(/(\.\d\d)0$/, '$1')} 鱼` : '—'; };
  const statusText = {pending:'正在排队生成自动复盘…',running:'正在生成自动复盘…',interrupted:'自动复盘已中断',failed:'自动复盘失败',unavailable:'自动复盘不可用',schema_validated:'自动复盘已校验',no_change:'没有建议调整',diagnostic_only:'仅诊断，不提供调整'};
  const runStatus = {running:'运行中',paused:'已暂停',ready:'待开始',completed:'已完成',collapsed:'已坍缩',error:'运行错误',failed:'运行失败'};
  let panel;
  function item(label, value) { const row = document.createElement('div'); row.className = 'review-item'; const key = document.createElement('strong'); key.textContent = label; row.append(key, document.createTextNode(` ${value === undefined || value === null ? '—' : value}`)); return row; }
  function ensurePanel() {
    if (panel) return panel;
    const old = byId('review'); old.textContent = '';
    panel = document.createElement('div'); panel.id = 'review-report';
    old.append(panel);
    return panel;
  }
  function section(title, kicker) { const box = document.createElement('section'); box.className = 'review-box'; const k = document.createElement('div'); k.className = 'kicker'; k.textContent = kicker; const h = document.createElement('h3'); h.textContent = title; box.append(k, h); return box; }
  function render(data, historical = false) {
    const root = ensurePanel(); root.textContent = '';
    const facts = data.facts_report || data.metrics || {}; const metrics = facts.metrics || facts;
    const factBox = section('事实报告', '规则与账本记录 · 可核对');
    const rows = [['食物生产', metrics.food_produced ?? metrics.production ?? metrics.total_production, true], ['食物消耗', metrics.food_consumed ?? metrics.consumption ?? metrics.total_consumed, true], ['当前存粮', metrics.final_food ?? metrics.food_stock ?? metrics.stock, true], ['活跃居民', metrics.active_residents ?? metrics.final_active ?? metrics.active, false], ['运行天数', metrics.days ?? metrics.completed_days ?? metrics.elapsed_days, false], ['交易次数', metrics.trades ?? metrics.trade_count, false], ['渔网：做出', metrics.nets_made, false], ['渔网：当前可用', metrics.final_nets ?? metrics.nets_live, false], ['净变化（存粮）', metrics.net_food_change ?? metrics.net_change, true], ['短缺', metrics.shortage ?? metrics.shortages, true]];
    rows.forEach(([label, value, isFish]) => { if (value !== undefined) factBox.append(item(label, isFish ? fish(value) : value)); });
    if (!factBox.querySelector('.review-item')) factBox.append(item('状态', '事实报告尚未生成。'));
    const llm = historical ? null : data.llm_review; const llmBox = section('自动复盘', '模型解释 · 不替代事实');
    if (!llm) llmBox.append(item('状态', historical ? '当前为历史快照，请回到最新查看最终复盘。' : '运行完成后，这里会自动显示复盘。')); else {
      llmBox.append(item('状态', statusText[llm.status] || llm.status || '等待报告'));
      if (['pending', 'running'].includes(llm.status)) llmBox.append(item('进度', '报告生成中，页面会继续更新。'));
      if (['failed','unavailable','interrupted'].includes(llm.status)) { llmBox.append(item('原因', llm.error_detail?.message || llm.error || '未提供详细原因')); if (llm.blocked_reason) llmBox.append(item('处理', llm.blocked_reason)); }
      if (llm.summary) llmBox.append(item('摘要', llm.summary));
      const claims = Array.isArray(llm.claims) ? llm.claims : Object.entries(llm.claims || {}).map(([metric_id, value]) => Object.assign({metric_id}, value));
      if (claims.length) { const h = document.createElement('h4'); h.textContent = '可核对的判断'; llmBox.append(h); claims.forEach(claim => llmBox.append(item(claim.metric_id || '指标', `${['final_food','food_produced','food_consumed','shortage','initial_food'].includes(claim.metric_id) ? fish(claim.value) : (claim.value ?? '—')}${claim.interpretation ? ` · ${claim.interpretation}` : ''}${claim.evidence_ids?.length ? `（证据：${claim.evidence_ids.join('、')}）` : ''}`))); }
      const proposal = llm.proposal || llm.candidate;
      if (proposal) {
        const h = document.createElement('h4'); h.textContent = '调整建议（尚未应用）'; llmBox.append(h);
        const cfg = data.run?.config || {};
        if (proposal.initial_fish_units_delta !== undefined) {
          const current = cfg.initial_fish_units;
          llmBox.append(item('每人初始存粮', `${fish(current)} → ${fish(Number(current) + proposal.initial_fish_units_delta)}`), item('变化', fish(proposal.initial_fish_units_delta)));
        }
        if (proposal.fishing_abundance_percent !== undefined) llmBox.append(item('捕鱼丰度', `${cfg.fishing_abundance_percent}% → ${proposal.fishing_abundance_percent}%`));
        if (proposal.hypothesis) llmBox.append(item('假设', proposal.hypothesis));
        if (proposal.risk) llmBox.append(item('风险', proposal.risk));
        llmBox.append(item('后续', llm.eligible_for_screening ? '建议进入配对实验验证，尚未采纳。' : '仅供诊断，暂不进入参数验证。'));
      }
      else if (llm.status === 'no_change') llmBox.append(item('结论', '没有建议调整。'));
      else if (llm.status === 'diagnostic_only') llmBox.append(item('结论', '仅诊断，不提供调整。'));
      if (llm.blocked_reason) llmBox.append(item('未执行原因', llm.blocked_reason));
    }
    root.append(factBox, llmBox);
  }
  window.IsleworksReview = {render};
})();
