(() => {
  'use strict';

  const state = { runs: [], runId: null, rows: [], asOf: null, lastCommitted: null, request: 0, mode: 'planned', pollTimer: null, status: null };
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const requestedRun = params.get('run');
  const requestedDay = params.get('day');
  const colors = ['#168a87', '#287a9e', '#c17655', '#8b6fb3', '#d29c48', '#5c8a65', '#a85b70'];
  const activityNames = { fish: '捕鱼', fishing: '捕鱼', gather: '采集', gathering: '采集', craft: '制造', crafting: '制造', rest: '休息', resting: '休息' };

  function esc(value) { return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function number(value) { return value === null || value === undefined || value === '' || Number.isNaN(Number(value)) ? null : Number(value); }
  function fish(value) { const n = number(value); return n === null ? '—' : (n / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 }); }
  function count(value) { const n = number(value); return n === null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }); }
  function activityLabel(key) { return activityNames[String(key).toLowerCase()] || String(key); }
  function normalizeActivity(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).map(([key, val]) => [key, number(val)]));
  }
  function visibleRows() { return state.rows.filter((row) => number(row.day) !== null && number(row.day) <= Number(state.asOf)); }
  function showError(message) { $('error').textContent = message; $('error').classList.remove('hidden'); $('loading').classList.add('hidden'); }
  function hideError() { $('error').classList.add('hidden'); }
  function updateUrl(day) {
    const url = new URL(location.href); if (state.runId) url.searchParams.set('run', state.runId);
    if (day === 'last' || day === null || Number(day) >= Number(state.lastCommitted)) url.searchParams.delete('day'); else url.searchParams.set('day', String(day));
    history.replaceState(null, '', url); $('home-link').href = `/?run=${encodeURIComponent(state.runId || '')}${url.searchParams.has('day') ? `&day=${encodeURIComponent(url.searchParams.get('day'))}` : ''}`;
  }
  function setStatus(text) { $('run-status').textContent = text; }
  function svgNode(tag, attrs = {}) { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value)); return node; }
  function renderChart(targetId, rows, series, unit) {
    const target = $(targetId); target.replaceChildren();
    const available = series.filter((item) => rows.some((row) => number(row[item.key]) !== null));
    if (!rows.length || !available.length) { const empty = document.createElement('div'); empty.className = 'chart-empty'; empty.textContent = '当前没有可用记录'; target.append(empty); return; }
    const width = 720; const height = 218; const left = 40; const right = 10; const top = 14; const bottom = 27;
    const all = available.flatMap((item) => rows.map((row) => number(row[item.key])).filter((v) => v !== null));
    let min = Math.min(...all); let max = Math.max(...all); if (min === max) { min = Math.min(0, min); max = max === 0 ? (unit === '鱼' ? 1000 : 1) : max + 1; } else if (min > 0) min = 0;
    const x = (index) => left + (rows.length === 1 ? (width - left - right) / 2 : index * (width - left - right) / (rows.length - 1));
    const y = (value) => top + (max - value) * (height - top - bottom) / (max - min);
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': target.getAttribute('aria-label') || '统计图' });
    for (let i = 0; i < 4; i += 1) { const value = min + (max - min) * i / 3; const yy = y(value); svg.append(svgNode('line', { x1: left, x2: width - right, y1: yy, y2: yy, stroke: '#dce6e1', 'stroke-width': 1 })); const text = svgNode('text', { x: left - 6, y: yy + 4, 'text-anchor': 'end', fill: '#64828a', 'font-size': 10 }); text.textContent = unit === '鱼' ? (value / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 }) : Number(value.toFixed(2)).toLocaleString('zh-CN'); svg.append(text); }
    const first = rows[0].day; const last = rows[rows.length - 1].day;
    [0, rows.length - 1].forEach((index) => { const text = svgNode('text', { x: x(index), y: height - 6, 'text-anchor': index ? 'end' : 'start', fill: '#64828a', 'font-size': 10 }); text.textContent = `第${rows[index].day}日`; svg.append(text); });
    available.forEach((item, seriesIndex) => {
      let path = ''; let drawing = false;
      rows.forEach((row, index) => { const value = number(row[item.key]); if (value === null) { drawing = false; return; } path += `${drawing ? ' L' : 'M'}${x(index).toFixed(1)} ${y(value).toFixed(1)}`; drawing = true; });
      if (path) svg.append(svgNode('path', { d: path, fill: 'none', stroke: item.color || colors[seriesIndex % colors.length], 'stroke-width': 2.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      rows.forEach((row,index)=>{const value=number(row[item.key]);if(value===null)return;const point=svgNode('circle',{cx:x(index),cy:y(value),r:3,fill:item.color||colors[seriesIndex%colors.length]});const tip=svgNode('title');tip.textContent=`第${row.day}日 · ${item.label}：${unit==='鱼'?fish(value):count(value)} ${unit}`;point.append(tip);svg.append(point);});
    });
    target.append(svg); const legend = document.createElement('div'); legend.className = 'legend'; available.forEach((item, index) => { const span = document.createElement('span'); span.innerHTML = `<i style="background:${item.color || colors[index % colors.length]}"></i>${esc(item.label)}`; legend.append(span); }); target.append(legend);
  }
  function renderCards(rows) {
    const row = rows[rows.length - 1] || {}; const cards = [['食物存量', fish(row.food_stock), '鱼'], ['活跃居民', count(row.active), '人'], ['交易次数', count(row.trades), '笔'], ['缺粮', fish(row.shortage), '鱼']];
    $('stat-cards').replaceChildren(...cards.map(([title, value, unit]) => { const card = document.createElement('article'); card.className = 'panel stat-card'; card.innerHTML = `<div class="kicker">${esc(title)}</div><div class="value">${esc(value)}</div><small>${esc(unit)} · 第${esc(row.day ?? '—')}日</small>`; return card; }));
  }
  function renderTable(rows) {
    const headers = [['day', '日'], ['food_stock', '存量（鱼）'], ['food_produced', '产出（鱼）'], ['food_consumed', '消耗（鱼）'], ['shortage', '缺粮（鱼）'], ['active', '活跃人数'], ['trades', '交易'], ['nets_made', '渔网']];
    $('data-table').querySelector('thead').innerHTML = `<tr>${headers.map(([, label]) => `<th scope="col">${label}</th>`).join('')}</tr>`;
    $('data-table').querySelector('tbody').innerHTML = rows.map((row) => `<tr>${headers.map(([key]) => `<td>${key === 'day' ? esc(row[key] ?? '—') : esc(['food_stock', 'food_produced', 'food_consumed', 'shortage'].includes(key) ? fish(row[key]) : count(row[key]))}</td>`).join('')}</tr>`).join('');
  }
  function renderActivity(rows) {
    const key = state.mode === 'planned' ? 'planned_activity_counts' : 'executed_labor_blocks'; const merged = new Map(); rows.forEach((row) => { if (row[key] && typeof row[key] === 'object' && !Array.isArray(row[key])) Object.keys(row[key]).forEach((name) => merged.set(name, true)); });
    const series = [...merged.keys()].map((name, index) => ({ key: `activity_${index}`, label: activityLabel(name), color: colors[index % colors.length] })); const chartRows = rows.map((row) => { const available = row[key] && typeof row[key] === 'object' && !Array.isArray(row[key]); const activity = normalizeActivity(row[key]); const copy = { day: row.day }; [...merged.keys()].forEach((name, index) => { copy[`activity_${index}`] = available ? (activity[name] ?? 0) : null; }); return copy; });
    renderChart('activity-chart', chartRows, series, state.mode === 'planned' ? '人' : '时段'); $('activity-unit').textContent = state.mode === 'planned' ? '单位：人（计划主要活动人数）' : '单位：劳动时段（实际执行）';
    const names=[...merged.keys()], table=$('activity-table');
    table.querySelector('thead').innerHTML=`<tr><th scope="col">日</th>${names.map(name=>`<th scope="col">${esc(activityLabel(name))}</th>`).join('')}</tr>`;
    table.querySelector('tbody').innerHTML=rows.map(row=>`<tr><td>${esc(row.day)}</td>${names.map(name=>{const mapping=row[key];const value=mapping && typeof mapping==='object'&&!Array.isArray(mapping)?(mapping[name]??0):null;return `<td>${esc(count(value))}</td>`;}).join('')}</tr>`).join('');
  }
  function render(rows) {
    renderCards(rows); renderChart('food-chart', rows, [{ key: 'food_stock', label: '食物存量', color: colors[0] }], '鱼'); renderChart('flow-chart', rows, [{ key: 'food_produced', label: '产出', color: colors[1] }, { key: 'food_consumed', label: '消耗', color: colors[2] }], '鱼'); renderChart('people-chart', rows, [{ key: 'active', label: '活跃居民', color: colors[0] }], '人'); renderChart('shortage-chart', rows, [{ key: 'shortage', label: '缺粮', color: colors[2] }], '鱼'); renderActivity(rows); renderTable(rows);
    $('day-output').textContent = state.asOf ?? '—'; $('day-slider').value = state.asOf ?? 0; $('snapshot-note').textContent = Number(state.asOf) < Number(state.lastCommitted) ? `历史快照：第 ${state.asOf} 日（最新已提交至第 ${state.lastCommitted} 日）` : `最新快照：第 ${state.asOf} 日 · 指标版本 ${state.metricVersion || '—'}`;
  }
  async function fetchJson(url) { const response = await fetch(url, { headers: { Accept: 'application/json' } }); if (!response.ok) throw new Error(`请求失败（${response.status}）`); return response.json(); }
  async function loadRuns() { const payload = await fetchJson('/api/runs'); state.runs = Array.isArray(payload) ? payload : (payload.runs || []); const select = $('run-select'); select.replaceChildren(); state.runs.forEach((run) => { const option = document.createElement('option'); option.value = run.run_id || run.id; option.textContent = `${String(option.value).slice(0, 10)} · ${run.scenario || run.name || '规则运行'}`; select.append(option); }); if (!state.runs.length) throw new Error('暂无可用运行'); state.runId = state.runs.some((run) => (run.run_id || run.id) === requestedRun) ? requestedRun : (state.runs[0].run_id || state.runs[0].id); select.value = state.runId; }
  function stopPolling() { if (state.pollTimer) clearInterval(state.pollTimer); state.pollTimer = null; }
  function syncPolling(day) { const latest = day === 'last' || Number(day) >= Number(state.lastCommitted); const running = ['running', 'active', 'ready'].includes(state.status); if (!latest || !running) { stopPolling(); return; } if (!state.pollTimer) { let busy=false; state.pollTimer = setInterval(async () => { if (busy) return; busy=true; try { await safeLoad('last'); } finally { busy=false; } }, 3000); } }
  async function loadStatistics(day = 'last') { const request = ++state.request; hideError(); $('loading').classList.remove('hidden'); $('dashboard').classList.add('hidden'); setStatus('读取中…'); const payload = await fetchJson(`/api/runs/${encodeURIComponent(state.runId)}/statistics?day=${encodeURIComponent(day)}`); if (request !== state.request) return false; state.rows = Array.isArray(payload.daily) ? payload.daily : []; state.asOf = number(payload.as_of_day); state.lastCommitted = number(payload.last_committed_day) ?? state.asOf; state.metricVersion = payload.metric_version; state.status = payload.status; const slider = $('day-slider'); slider.disabled = state.lastCommitted === null; slider.max = state.lastCommitted ?? 0; render(visibleRows()); $('loading').classList.add('hidden'); $('dashboard').classList.remove('hidden'); setStatus(day === 'last' || Number(day) >= state.lastCommitted ? '最新已提交' : '历史快照'); updateUrl(day); syncPolling(day); return true; }
  async function safeLoad(day) { const before = state.request; try { await loadStatistics(day); } catch (error) { if (state.request !== before + 1) return; showError(error.message || '统计资料读取失败'); setStatus('读取失败'); } }
  $('run-select').addEventListener('change', () => { stopPolling(); state.runId = $('run-select').value; state.asOf = null; safeLoad('last'); });
  $('day-slider').addEventListener('change', () => { stopPolling(); safeLoad($('day-slider').value); }); $('latest').addEventListener('click', () => safeLoad('last'));
  document.querySelectorAll('.toggle button').forEach((button) => button.addEventListener('click', () => { state.mode = button.dataset.mode; document.querySelectorAll('.toggle button').forEach((item) => item.classList.toggle('active', item === button)); renderActivity(visibleRows()); }));
  (async () => { try { await loadRuns(); const initialDay = requestedDay && /^\d+$/.test(requestedDay) ? requestedDay : 'last'; await safeLoad(initialDay); } catch (error) { showError(error.message || '运行列表读取失败'); } })();
})();
