(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const fish = value => typeof value === 'number' && Number.isFinite(value)
    ? `${(value / 1000).toFixed(3).replace(/\.?0+$/, '')} 鱼` : '—';
  const statusText = {pending:'正在准备自动复盘',running:'正在生成自动复盘',interrupted:'复盘已中断',failed:'复盘失败',unavailable:'复盘不可用',schema_validated:'复盘完成 · 建议待验证',no_change:'复盘完成 · 未建议调整',diagnostic_only:'复盘完成 · 仅供诊断'};
  const metricText = {food_produced:'食物生产',food_consumed:'食物消耗',final_food:'最终存粮',shortage:'缺粮量',shortage_days:'居民短缺次数',final_active:'活跃居民',trades:'交易次数',nets_made:'制作渔网',nets_broken:'损坏渔网',invalid_actions:'无效动作'};
  const fishMetrics = new Set(['food_produced','food_consumed','final_food','shortage','initial_food']);
  function section(title, label) {
    const box = document.createElement('section'); box.className = 'review-box';
    const k = document.createElement('div'); k.className = 'kicker'; k.textContent = label;
    const h = document.createElement('h3'); h.textContent = title; box.append(k,h); return box;
  }
  function item(label, value) {
    const row = document.createElement('div'); row.className = 'review-item';
    const strong = document.createElement('strong'); strong.textContent = label;
    row.append(strong, document.createTextNode(` ${value ?? '—'}`)); return row;
  }
  function heading(box, label) { const h = document.createElement('h4'); h.textContent = label; box.append(h); }
  function render(data, historical = false) {
    const container = byId('review'); if (!container) return;
    container.textContent = '';
    const root = document.createElement('div'); root.id = 'review-report'; container.append(root);
    const report = data.facts_report, selectedMetrics = data.metrics?.metrics || {};
    const metrics = report?.metrics || selectedMetrics;
    const factBox = section(historical ? '截至当日的统计' : '事实报告', '程序计算 · 可核对');
    const rows = [['食物生产','food_produced',true],['食物消耗','food_consumed',true],['当前存粮','final_food',true],['活跃居民','final_active',false],['运行天数','completed_days',false],['交易次数','trades',false],['渔网：制作','nets_made',false],['渔网：当前可用','final_nets',false],['缺粮量','shortage',true]];
    for (const [label,key,isFish] of rows) if (metrics[key] !== undefined && metrics[key] !== null)
      factBox.append(item(label, isFish ? fish(metrics[key]) : metrics[key]));
    if (!factBox.querySelector('.review-item')) factBox.append(item('状态','事实报告尚未生成。'));
    const llmBox = section('本轮经济复盘', '模型解释 · 不替代事实');
    if (data.run?.id) llmBox.append(item('运行', data.run.id));
    const llm = historical ? null : data.llm_review;
    if (!llm) {
      const terminal = ['completed','collapsed'].includes(data.run?.status);
      llmBox.append(item('状态', historical && terminal ? '正在回看历史；最终复盘基于整轮，回到最新可查看。' : '轮次结束后按配置自动生成。'));
    } else {
      llmBox.append(item('状态',statusText[llm.status] || llm.status || '等待报告'));
      if (['pending','running'].includes(llm.status)) llmBox.append(item('进度','报告生成中，页面会继续更新。'));
      if (['failed','unavailable','interrupted'].includes(llm.status)) llmBox.append(item('原因',llm.error_detail?.message || llm.error || '未提供详细原因。'));
      if (llm.summary) { heading(llmBox,'本轮总结'); llmBox.append(item('模型解释',llm.summary)); }
      if (Array.isArray(llm.claims) && llm.claims.length) {
        heading(llmBox,'主要观察');
        for (const claim of llm.claims) {
          const value = fishMetrics.has(claim.metric_id) ? fish(claim.value) : (claim.value ?? '—');
          llmBox.append(item(metricText[claim.metric_id] || claim.metric_id, `${value} · ${claim.interpretation || ''}`));
          if (claim.evidence_ids?.length) llmBox.append(item('依据事件',claim.evidence_ids.join('、')));
        }
      }
      const proposal = llm.proposal;
      if (proposal) {
        heading(llmBox,'下一轮资源建议'); const cfg = data.run?.config || {};
        if (typeof proposal.initial_fish_units_delta === 'number') {
          const current=cfg.initial_fish_units;
          llmBox.append(item('每人初始存粮', typeof current === 'number' ? `${fish(current)} → ${fish(current+proposal.initial_fish_units_delta)}` : `调整 ${fish(proposal.initial_fish_units_delta)}`));
        }
        if (proposal.fishing_abundance_percent !== undefined) llmBox.append(item('捕鱼丰度',`${cfg.fishing_abundance_percent ?? '—'}% → ${proposal.fishing_abundance_percent}%`));
        if (proposal.hypothesis) llmBox.append(item('假设',proposal.hypothesis));
        if (proposal.risk) llmBox.append(item('风险',proposal.risk));
        if (proposal.evidence_ids?.length) llmBox.append(item('依据事件',proposal.evidence_ids.join('、')));
        llmBox.append(item('后续',llm.eligible_for_screening ? '建议进入配对实验验证，尚未采纳。' : '仅供诊断，暂不进入参数验证。'));
      } else if (['no_change','schema_validated','diagnostic_only'].includes(llm.status)) llmBox.append(item('建议','本轮未提出参数调整建议。'));
      if (llm.blocked_reason) llmBox.append(item('受限原因',llm.blocked_reason));
    }
    if (data.run?.id) {
      const link=document.createElement('a');link.className='review-link';
      const params=new URLSearchParams({run:data.run.id}); if (historical && data.state?.day !== undefined) params.set('day',data.state.day);
      link.href=`/statistics.html?${params}`;link.textContent='查看数据统计 →';factBox.append(link);
    }
    root.append(factBox,llmBox);
  }
  window.IsleworksReview = {render};
})();
