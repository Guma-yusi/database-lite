const osButton = document.querySelector('#os-nav');
const osUIState = { selectedPage: null };

const OS_POLICY_GUIDE = {
  FIFO: { title: 'FIFO', summary: '谁先进缓存，谁先被淘汰。', focus: '适合对比“进入顺序”和“访问顺序”的差别。' },
  LRU: { title: 'LRU', summary: '最近最少使用的页优先淘汰。', focus: '重点观察命中后页是否仍然留在缓存。' },
  LFU: { title: 'LFU', summary: '访问次数最少的页优先淘汰。', focus: '重点观察 Buffer Frames 里的 Hits 计数。' },
  CLOCK: { title: 'CLOCK', summary: '用引用位给热点页第二次机会。', focus: '重点观察 Ref 位和日志中的 clock-scan。' },
};

const OS_EVENT_GUIDE = [
  ['miss', '缓存未命中，发生真实读盘'],
  ['hit', '缓存命中，直接从内存返回'],
  ['prefetch', '顺序扫描提前装入下一页'],
  ['replace', '缓存满后执行替换'],
  ['flush', '脏页写回磁盘'],
];

osButton.onclick = async () => {
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button === osButton));
  document.querySelector('#title').textContent = 'OS 存储仿真台';
  document.querySelector('#subtitle').textContent = '观察页目录、内存缓存、WAL 屏障、预读与检查点';
  document.querySelector('#primary').style.display = 'none';
  document.querySelector('#content').innerHTML = '<p>正在读取仿真状态…</p>';
  await renderOS();
};

function summarizeEvents(events) {
  return events.reduce((acc, event) => {
    acc[event.event] = (acc[event.event] || 0) + 1;
    return acc;
  }, {});
}

function percentage(value, total) {
  return total ? `${((value / total) * 100).toFixed(0)}%` : '0%';
}

function renderEventLines(events) {
  if (!events.length) return '尚无事件';
  return events.map(event => {
    const parts = [`#${String(event.sequence ?? '').padStart(3, '0')}`, event.event, `page=${event.page_id}`];
    if (event.victim_page_id !== undefined) parts.push(`victim=${event.victim_page_id}`);
    if (event.new_page_id !== undefined) parts.push(`new=${event.new_page_id}`);
    if (event.hits !== undefined) parts.push(`hits=${event.hits}`);
    if (event.lsn) parts.push(`lsn=${event.lsn}`);
    if (event.flushed !== undefined) parts.push(`flushed=${event.flushed ? 'Y' : 'N'}`);
    return parts.join('  ');
  }).join('\n');
}

function renderHexRows(detail) {
  if (!detail.hex_rows.length) {
    return '<div class="hex-empty">当前页未分配，暂无十六进制预览。</div>';
  }
  return detail.hex_rows.map((row, index) => {
    const offset = (index * 16).toString(16).padStart(4, '0').toUpperCase();
    return `<div class="hex-row"><span class="hex-offset">${offset}</span><code>${row}</code><span class="hex-ascii">${esc(detail.ascii_rows[index] || '')}</span></div>`;
  }).join('');
}

async function renderOS() {
  try {
    document.querySelector('#primary').style.display = 'none';
    const data = await api('/api/os/status');
    const stats = data.stats;
    const cached = new Set(data.frames.map(frame => frame.page_id));
    const selectedPage = osUIState.selectedPage ?? data.frames[0]?.page_id ?? data.pages.find(page => page.allocated)?.page_id ?? 0;
    osUIState.selectedPage = selectedPage;
    const detail = await api(`/api/os/page?page_id=${selectedPage}`);
    const policy = OS_POLICY_GUIDE[stats.policy] || { title: stats.policy, summary: '当前策略说明不可用。', focus: '' };
    const eventSummary = summarizeEvents(data.events);
    const allocated = data.pages.filter(page => page.allocated).length;
    const resident = data.frames.length;
    const dirty = data.frames.filter(frame => frame.dirty).length;
    const free = Math.max(data.pages.length - allocated, 0);
    const metrics = [
      ['命中率', (stats.hit_rate * 100).toFixed(1) + '%'],
      ['脏页', stats.dirty],
      ['磁盘读取', stats.disk_reads],
      ['WAL LSN', stats.wal_durable_lsn],
      ['Checkpoint', stats.checkpoint_lsn],
    ];
    const policyOptions = data.policies.map(policyName => `<option value="${policyName}" ${policyName === stats.policy ? 'selected' : ''}>${policyName}</option>`).join('');
    const pages = data.pages.map(page => `<button type="button" title="Page ${page.page_id} · offset ${page.offset}" class="page-cell ${page.allocated ? 'used' : ''} ${cached.has(page.page_id) ? 'cached' : ''} ${page.page_id === detail.page_id ? 'selected' : ''}" data-page-id="${page.page_id}"><span>${page.page_id}</span></button>`).join('');
    const frames = data.frames.map(frame => `<tr class="os-frame-row ${frame.page_id === detail.page_id ? 'selected' : ''}" data-page-id="${frame.page_id}"><td>${frame.page_id}</td><td>${frame.generation}</td><td>${frame.dirty ? '是' : '否'}</td><td>${frame.pin_count}</td><td>${frame.hits}</td><td>${frame.ref_bit}</td><td>${frame.page_lsn}</td></tr>`);
    const distribution = [
      ['已分配页', allocated, data.pages.length, 'tone-ink'],
      ['驻留缓存', resident, stats.capacity, 'tone-red'],
      ['脏页', dirty, Math.max(resident, 1), 'tone-gold'],
      ['未分配页', free, data.pages.length, 'tone-sage'],
    ];
    document.querySelector('#content').innerHTML = `<div class="os-shell">
      <div class="os-metrics">
        <div class="os-metric os-policy-metric"><small>缓存策略</small><b>${stats.policy}</b><label class="policy-select-wrap"><span>切换策略</span><select class="policy-select" data-policy-select aria-label="缓存策略切换">${policyOptions}</select></label></div>
        ${metrics.map(item => `<div class="os-metric"><small>${item[0]}</small><b>${item[1]}</b></div>`).join('')}
      </div>
      <section class="os-panel">
        <div class="section-head"><h3>页目录 · ${stats.allocated_pages}/${stats.total_pages} 已分配</h3><div class="os-actions">
          <button class="secondary" data-os="allocate">分配页</button><button class="secondary" data-os="write">修改内存页</button><button class="primary" data-os="checkpoint">Checkpoint</button><button class="secondary" data-os="workload">运行 1000 查询</button><button class="secondary danger" data-os="reset">重置仿真台</button>
        </div></div>
        <div class="page-map">${pages}</div>
        <div class="os-legend">
          <span class="legend-chip"><i class="legend-dot allocated"></i>已分配页</span>
          <span class="legend-chip"><i class="legend-dot cached"></i>缓存驻留</span>
          <span class="legend-chip"><i class="legend-dot selected"></i>当前选中页</span>
        </div>
        <h3>Buffer Frames</h3>
        ${table(['Page', 'Generation', 'Dirty', 'Pin', 'Hits', 'Ref', 'Page LSN'], frames)}
        <p class="os-note">点页格子或缓存帧即可查看右侧详情。LFU 重点观察 Hits，CLOCK 重点观察 Ref，Prefetch 会在顺序扫描时提前把 next_page 装入缓存。</p>
      </section>
      <div class="os-grid">
        <section class="os-panel">
          <h3>策略说明</h3>
          <div class="os-policy-card">
            <div>
              <small>当前算法</small>
              <strong>${policy.title}</strong>
            </div>
            <p>${policy.summary}</p>
            <p class="muted">${policy.focus}</p>
          </div>
          <div class="os-distribution">
            ${distribution.map(item => `<div class="dist-row"><div class="dist-head"><span>${item[0]}</span><b>${item[1]}</b><small>${percentage(item[1], item[2])}</small></div><div class="dist-bar"><span class="${item[3]}" style="width:${percentage(item[1], item[2])}"></span></div></div>`).join('')}
          </div>
        </section>
        <section class="os-panel os-detail-panel">
          <h3>页诊断 · Page ${detail.page_id}</h3>
          <div class="os-detail-grid">
            <div><small>偏移</small><b>${detail.offset}</b></div>
            <div><small>Generation</small><b>${detail.generation}</b></div>
            <div><small>Checksum</small><b>${detail.checksum}</b></div>
            <div><small>状态</small><b>${detail.allocated ? (detail.cached ? '已分配 / 已缓存' : '已分配 / 未缓存') : '未分配'}</b></div>
          </div>
          <div class="os-preview-copy">${esc(detail.preview_text || '当前页前 128B 为空，或尚未写入可打印文本。')}</div>
          <div class="hex-panel">${renderHexRows(detail)}</div>
        </section>
      </div>
      <section class="os-panel">
          <h3>缓存事件</h3>
          <div class="event-summary">
            ${Object.entries(eventSummary).map(([name, count]) => `<span class="event-pill">${name} · ${count}</span>`).join('') || '<span class="event-pill">暂无事件</span>'}
          </div>
          <pre class="event-log">${renderEventLines(data.events)}</pre>
          <div class="event-guide">
            ${OS_EVENT_GUIDE.map(item => `<div><small>${item[0]}</small><span>${item[1]}</span></div>`).join('')}
          </div>
      </section>
    </div>`;
    document.querySelector('#primary').style.display = 'none';
    document.querySelectorAll('[data-os]').forEach(button => button.onclick = () => osAction(button.dataset.os, data));
    document.querySelectorAll('[data-page-id]').forEach(node => {
      node.onclick = async () => {
        osUIState.selectedPage = Number(node.dataset.pageId);
        await renderOS();
      };
    });
    const select = document.querySelector('[data-policy-select]');
    if (select) select.onchange = event => switchPolicy(event.target.value);
  } catch (error) {
    document.querySelector('#content').innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

async function switchPolicy(policy) {
  try {
    const data = await api('/api/os/action', { method: 'POST', body: JSON.stringify({ action: 'set_policy', policy }) });
    toast(data.message || `缓存策略已切换为 ${policy}`);
    await renderOS();
  } catch (error) { toast(error.message); }
}

async function osAction(action, state) {
  if (action === 'reset' && !confirm('确认重置 OS 仿真台？这会清空当前页目录、缓存、WAL 与事件日志。')) return;
  const body = { action };
  if (action === 'reset') body.policy = state.stats.policy;
  if (action === 'write') {
    const used = state.pages.find(page => page.allocated);
    if (!used) {
      await api('/api/os/action', { method: 'POST', body: JSON.stringify({ action: 'allocate' }) });
      return osAction('write', await api('/api/os/status'));
    }
    body.page_id = used.page_id;
    body.text = 'memory update ' + new Date().toISOString();
    osUIState.selectedPage = used.page_id;
  }
  try {
    const data = await api('/api/os/action', { method: 'POST', body: JSON.stringify(body) });
    toast(action === 'workload' ? `1000 个查询完成，最大队列 ${data.result.max_depth}` : action === 'reset' ? (data.message || 'OS 仿真台已重置') : '操作完成');
    await renderOS();
  } catch (error) { toast(error.message); }
}

document.querySelectorAll('.nav:not(#os-nav):not(#lab-nav)').forEach(button => button.addEventListener('click', () => document.querySelector('#primary').style.display = ''));
if (new URLSearchParams(location.search).has('os')) setTimeout(() => osButton.click(), 180);
