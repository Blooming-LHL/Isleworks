(() => {
  'use strict';
  const activities = {
    fish: { label: '捕鱼', color: '#287a9e', x: 115, y: 315 },
    gather: { label: '采集', color: '#4f8b68', x: 600, y: 235 },
    craft: { label: '制造', color: '#b77b4e', x: 470, y: 355 },
    rest: { label: '休息', color: '#c49b5d', x: 310, y: 220 },
    other: { label: '其他', color: '#7d8990', x: 355, y: 300 }
  };
  const slots = ['morning', 'afternoon', 'evening', 'night'];
  const labels = { fish: ['fish', 'fishing', '捕鱼'], gather: ['gather', 'forage', 'gathering', '采集'], craft: ['craft', 'crafting', '制造'], rest: ['rest', '休息'] };
  const state = { data: null, reps: [], targets: [], frame: 0, selected: null, raf: 0 };
  const $ = id => document.getElementById(id);
  const residentList = data => Object.values(data?.state?.residents || {}).filter(Boolean);
  function activityOf(plan, day) {
    if (!plan || day <= 0) return 'other';
    let schedule = Array.isArray(plan.schedule) ? plan.schedule : [];
    const dayPlan = schedule.find(x => Number(x?.day) === day);
    const labor = Array.isArray(dayPlan?.labor) ? dayPlan.labor : [];
    if (!labor.length) return 'other';
    const counts = new Map();
    for (const action of labor.slice(0, 4)) counts.set(action, (counts.get(action) || 0) + 1);
    const max = Math.max(...counts.values());
    const primary = labor.find(action => counts.get(action) === max);
    return Object.keys(labels).find(key => labels[key].includes(String(primary).toLowerCase())) || 'other';
  }

  function representatives(residents) {
    if (residents.length <= 24) return residents;
    const output = [], step = residents.length / 24;
    for (let i = 0; i < 24; i += 1) output.push(residents[Math.min(residents.length - 1, Math.floor(i * step))]);
    return output;
  }
  function drawPixel(ctx, x, y, color, size = 1) { ctx.fillStyle = color; ctx.fillRect(Math.round(x), Math.round(y), size, size); }
  function background(ctx, tick) {
    ctx.fillStyle = '#c9e7df'; ctx.fillRect(0, 0, 800, 490);
    ctx.fillStyle = '#8fc2bd'; ctx.fillRect(0, 0, 800, 140);
    for (let i = 0; i < 16; i += 1) { const x = (i * 67 + tick * .35) % 850 - 40; drawPixel(ctx, x, 38 + (i % 4) * 21, '#b7d9d0', 3); }
    ctx.fillStyle = '#f2dca5'; ctx.fillRect(36, 128, 728, 326);
    ctx.fillStyle = '#ead092'; ctx.fillRect(36, 128, 728, 7);
    ctx.fillStyle = '#ded0a1'; ctx.fillRect(390, 137, 8, 317); ctx.fillRect(43, 294, 715, 8);
    ctx.fillStyle = '#c7dbc0'; ctx.fillRect(96, 350, 155, 76); ctx.fillRect(535, 145, 172, 86);
    ctx.fillStyle = '#d7b472'; ctx.fillRect(420, 255, 144, 100); ctx.fillStyle = '#8d6650'; ctx.fillRect(420, 255, 144, 12);
    ctx.fillStyle = '#f1e6bd'; ctx.fillRect(439, 281, 32, 74); ctx.fillRect(512, 281, 32, 74); ctx.fillStyle = '#9b6d51'; ctx.fillRect(475, 315, 24, 40);
    ctx.fillStyle = '#78a66f'; for (let i = 0; i < 14; i += 1) { const x = 61 + (i * 53) % 690; const y = 150 + (i * 71) % 280; ctx.fillRect(x, y, 9, 24); ctx.fillRect(x - 8, y + 4, 25, 10); }
    ctx.fillStyle = '#287a9e'; ctx.fillRect(0, 454, 800, 36); for (let i = 0; i < 24; i += 1) { const x = (i * 43 + tick * 1.5) % 820 - 20; ctx.fillRect(x, 465 + (i % 3) * 7, 14, 2); }
    ctx.fillStyle = '#355a58'; ctx.font = 'bold 12px ui-monospace, monospace'; ctx.fillText('ISLEWORKS / OBSERVER', 55, 45);
  }
  function sprite(ctx, resident, activity, i, tick) {
    const cfg = activities[activity], angle = (tick / 28 + i) % (Math.PI * 2), x = cfg.x + Math.sin(angle) * 22 + (i % 3) * 8, y = cfg.y + Math.cos(angle * .8) * 11 + Math.floor(i / 3) * 14;
    ctx.fillStyle = '#344b50'; ctx.fillRect(x - 5, y + 17, 10, 10); ctx.fillStyle = cfg.color; ctx.fillRect(x - 7, y + 7, 14, 12); ctx.fillStyle = '#f4bf84'; ctx.fillRect(x - 6, y, 12, 10); ctx.fillStyle = '#3d4a4b'; ctx.fillRect(x - 6, y - 2, 12, 4);
    state.targets.push({ x: x - 12, y: y - 6, w: 24, h: 36, resident, activity });
  }
  function updateSummary(residents, reps, day, population) {
    const counts = population ? population.counts : {}; if (!population) residents.forEach(x => { counts[x.activity] = (counts[x.activity] || 0) + 1; });
    const total = population?.total ?? residents.length, label = day <= 0 ? `第${day}日 · ${total}位居民 · 尚无计划` : `第${day}日 · ${total}位居民 · 显示${reps.length}位代表`;
    $('pixel-snapshot-label').textContent = label;
    $('pixel-counts').textContent = '';
    [['总人数', total], ['画面代表', reps.length], ['有计划', population ? (population.planned ?? '—') : residents.filter(x => x.hasPlan).length]].forEach(([name, value]) => { const item = document.createElement('div'); item.className = 'pixel-stat'; const strong = document.createElement('strong'); strong.textContent = value; const small = document.createElement('small'); small.textContent = name; item.append(strong, small); $('pixel-counts').append(item); });
    $('pixel-groups').textContent = '';
    Object.keys(activities).forEach(key => { const button = document.createElement('button'); button.type = 'button'; button.className = 'pixel-group-button'; const dot = document.createElement('i'); dot.style.background = activities[key].color; button.append(dot, document.createTextNode(`${activities[key].label} ${counts ? (counts[key] || 0) : '—'}`)); button.onclick = () => { const group = reps.filter(x => x.activity === key); $('pixel-selected').textContent = group.length ? `${activities[key].label}：${group.map(x => x.name || x.id).join('、')}` : `当前画面没有${activities[key].label}代表。`; }; $('pixel-groups').append(button); });
    if (total > reps.length) { const note = document.createElement('p'); note.className = 'pixel-note'; note.textContent = `居民总数为 ${total}，场景仅抽样显示 24 位代表。下方可按居民 ID 查找。`; $('pixel-groups').append(note); }
  }
  function draw() {
    const canvas = $('pixel-scene'); if (!canvas || !state.data) return;
    const ctx = canvas.getContext('2d'); state.frame += 1; state.targets = []; background(ctx, state.frame);
    state.reps.forEach((resident, index) => sprite(ctx, resident, resident.activity, index, state.frame));
    if (state.selected) { const hit = state.targets.find(x => x.resident.id === state.selected); if (hit) { ctx.strokeStyle = '#173b45'; ctx.lineWidth = 2; ctx.strokeRect(hit.x, hit.y, hit.w, hit.h); } }
    state.raf = requestAnimationFrame(draw);
  }
  function render(data, onSelect) {
    state.data = data || {}; state.onSelect = onSelect; const day = Number(state.data.state?.day ?? state.data.run?.day ?? 0), residents = residentList(state.data), plans = state.data.plans || {};
    const context = `${state.data.run?.id || ''}:${day}`; if (state.context !== context) { state.selected = null; state.context = context; $('pixel-selected').textContent = '点击画面中的居民或活动群体。'; }
    const enriched = residents.map(x => ({ ...x, activity: activityOf(plans[x.id] || plans[String(x.id)], day), hasPlan: Boolean((plans[x.id]?.schedule || []).some(p => p.day === day && p.labor?.length)) })); state.reps = representatives(enriched); updateSummary(enriched, state.reps, day, state.data.population); if (!state.raf) draw();
  }
  $('pixel-scene')?.addEventListener('click', event => {
    const canvas = $('pixel-scene'), rect = canvas.getBoundingClientRect(), x = (event.clientX - rect.left) * canvas.width / rect.width, y = (event.clientY - rect.top) * canvas.height / rect.height;
    const target = state.targets.find(hit => x >= hit.x && x <= hit.x + hit.w && y >= hit.y && y <= hit.y + hit.h);
    if (!target) return;
    state.selected = target.resident.id;
    $('pixel-selected').textContent = `${target.resident.name || target.resident.id} · ${activities[target.activity].label}（计划活动示意）`;
    state.onSelect?.(target.resident.id);
  });
  window.IsleworksPixel = { render };
})();
