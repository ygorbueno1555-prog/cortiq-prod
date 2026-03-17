/* ============================================================
   CORTIQ DECISION COPILOT — Frontend v3
   Claude-powered · Gap detection · Evidence trail · Comparison
   ============================================================ */

let currentMode  = 'equity';
let currentES    = null;
let reportBuffer = '';
let renderFrame  = null;
let startTime    = null;
let sourcesMap   = [];  // [{title, url, source_type}] indexed from 1

// ── DOM ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const equityForm    = $('equity-form');
const startupForm   = $('startup-form');
const statusPanel   = $('status-panel');
const statusDot     = $('status-dot');
const statusText    = $('status-text');
const queriesList   = $('queries-list');
const emptyState    = $('empty-state');
const verdictBar    = $('verdict-bar');
const verdictTag    = $('verdict-tag');
const verdictMeta   = $('verdict-meta');
const verdictTime   = $('verdict-time');
const reportWrapper = $('report-wrapper');
const reportContent = $('report-content');

// ── Mode tabs ─────────────────────────────────────────────
document.querySelectorAll('.mode-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    currentMode = tab.dataset.mode;
    document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    equityForm.style.display  = currentMode === 'equity'  ? 'block' : 'none';
    startupForm.style.display = currentMode === 'startup' ? 'block' : 'none';
    resetOutput();
  });
});

// ── Analyze buttons ───────────────────────────────────────
$('btn-equity').addEventListener('click', () => {
  const ticker  = $('ticker').value.trim().toUpperCase();
  const thesis  = $('thesis').value.trim();
  const mandate = $('mandate').value.trim();
  if (!ticker) { highlight('ticker'); return; }

  // Check history for comparison
  const prev = historyFind('equity', ticker);
  const prevVerdict = prev ? encodeURIComponent(prev.verdict || '') : '';
  const prevDate    = prev ? encodeURIComponent(prev.date ? new Date(prev.date).toLocaleDateString('pt-BR') : '') : '';

  startAnalysis(
    `/analyze/equity?ticker=${enc(ticker)}&thesis=${enc(thesis)}&mandate=${enc(mandate)}&prev_verdict=${prevVerdict}&prev_date=${prevDate}`,
    ticker
  );
});

$('btn-startup').addEventListener('click', () => {
  const name   = $('startup-name').value.trim();
  const url    = $('startup-url').value.trim();
  const thesis = $('startup-thesis').value.trim();
  if (!name) { highlight('startup-name'); return; }

  const prev = historyFind('startup', name);
  const prevVerdict = prev ? encodeURIComponent(prev.verdict || '') : '';
  const prevDate    = prev ? encodeURIComponent(prev.date ? new Date(prev.date).toLocaleDateString('pt-BR') : '') : '';

  startAnalysis(
    `/analyze/startup?name=${enc(name)}&url=${enc(url)}&thesis=${enc(thesis)}&prev_verdict=${prevVerdict}&prev_date=${prevDate}`,
    name
  );
});

// Enter shortcuts
$('ticker').addEventListener('keydown',       e => e.key === 'Enter' && $('btn-equity').click());
$('startup-name').addEventListener('keydown', e => e.key === 'Enter' && $('btn-startup').click());

// ── Start analysis ────────────────────────────────────────
function startAnalysis(url, label) {
  if (currentES) { currentES.close(); currentES = null; }

  resetOutput();
  reportBuffer = '';
  sourcesMap   = [];
  startTime    = Date.now();

  statusPanel.classList.add('visible');
  setStatus('Conectando...', 'pulse');

  emptyState.style.display = 'none';
  reportWrapper.classList.add('visible');
  verdictBar.classList.add('visible');
  verdictTag.textContent  = label.toUpperCase();
  verdictTag.className    = 'verdict-tag blue';
  verdictMeta.textContent = '';
  verdictTime.textContent = '';
  reportContent.innerHTML = '<div class="streaming-cursor cursor-blink"></div>';

  setBtnState(true);

  currentES = new EventSource(url);

  currentES.addEventListener('status', e => setStatus(e.data, 'pulse'));

  currentES.addEventListener('queries', e => {
    try { renderQueries(JSON.parse(e.data), 'initial'); } catch {}
  });

  currentES.addEventListener('followup_queries', e => {
    try { renderQueries(JSON.parse(e.data), 'followup'); } catch {}
  });

  currentES.addEventListener('sources', e => {
    try { sourcesMap = JSON.parse(e.data); } catch {}
  });

  currentES.addEventListener('market_data', e => {
    try { renderMarketDataBar(JSON.parse(e.data)); } catch {}
  });

  currentES.addEventListener('chunk', e => {
    reportBuffer += e.data;
    scheduleRender(false);
    detectVerdict(reportBuffer);
  });

  currentES.addEventListener('done', () => {
    if (currentES) { currentES.close(); currentES = null; }
    scheduleRender(true);
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    verdictTime.textContent = `${elapsed}s`;
    setStatus('Análise concluída', 'done');
    setBtnState(false);
    markQueriesDone();

    // Save to history
    const confMatch = reportBuffer.match(/Confiança:\s*\*?\*?([A-ZÁÉÍÓÚÃÕ]+)\*?\*?/i);
    const confidence = confMatch ? confMatch[1] : '';
    const mode  = currentMode;
    const key   = mode === 'equity'
      ? $('ticker').value.trim().toUpperCase()
      : $('startup-name').value.trim();
    const extras = mode === 'equity'
      ? { thesis: $('thesis').value.trim(), mandate: $('mandate').value.trim() }
      : { url: $('startup-url').value.trim(), thesis: $('startup-thesis').value.trim() };

    if (key) {
      historySaveCompleted(
        mode, key, reportBuffer,
        verdictTag.textContent, verdictTag.className.replace('verdict-tag ', ''),
        confidence, extras
      );
    }
  });

  currentES.addEventListener('error', e => {
    if (e.data) { reportBuffer += `\n\n## Erro\n${e.data}`; scheduleRender(true); }
  });

  currentES.onerror = () => {
    if (currentES?.readyState === EventSource.CLOSED) {
      setStatus('Conexão encerrada', 'error');
      setBtnState(false);
      currentES = null;
      scheduleRender(true);
    }
  };
}

// ── Render ────────────────────────────────────────────────
function scheduleRender(final) {
  if (renderFrame) cancelAnimationFrame(renderFrame);
  if (final) {
    renderBlocks(reportBuffer, true);
  } else {
    renderFrame = requestAnimationFrame(() => renderBlocks(reportBuffer, false));
  }
}

// Section color mapping
const SECTION_COLORS = {
  'VEREDITO':                       'green',
  'AÇÃO RECOMENDADA':               'green',
  'O QUE MUDOU':                    'amber',
  'CATALISADORES':                  'blue',
  'RISCOS':                         'red',
  'IMPACTO':                        'amber',
  'TRILHA DE EVIDÊNCIAS':           'purple',
  'RESUMO EXECUTIVO':               'blue',
  'TIME':                           'blue',
  'MERCADO':                        'amber',
  'TRAÇÃO':                         'green',
  'CONCORRENTES':                   'amber',
  'RED FLAGS':                      'red',
  'TESE DE INVESTIMENTO':           'blue',
  'GATILHOS':                       'red',
  'PRÓXIMOS PASSOS':                'purple',
  'EXPLORAR TAMBÉM':                'purple',
};

function sectionColor(title) {
  const upper = title.toUpperCase();
  for (const [key, color] of Object.entries(SECTION_COLORS)) {
    if (upper.includes(key)) return color;
  }
  return 'blue';
}

function renderBlocks(markdown, final) {
  if (!markdown) return;

  const rawSections = markdown.split(/(?=^## )/m);
  const blocks = [];

  rawSections.forEach(section => {
    const trimmed = section.trim();
    if (!trimmed) return;
    if (trimmed.startsWith('## ')) {
      const lines = trimmed.split('\n');
      const title = lines[0].replace(/^## /, '').trim();
      const body  = lines.slice(1).join('\n').trim();
      blocks.push({ title, body });
    } else {
      if (trimmed) blocks.push({ title: null, body: trimmed });
    }
  });

  if (!blocks.length) {
    reportContent.innerHTML =
      `<div class="report-section"><div class="report-section-body"><p>${escHtml(markdown)}</p></div></div>`
      + (final ? '' : '<div class="streaming-cursor cursor-blink"></div>');
    return;
  }

  let html = '';
  blocks.forEach((block, idx) => {
    const isLast    = idx === blocks.length - 1;
    const color     = block.title ? sectionColor(block.title) : 'blue';
    const isExplore = block.title?.toUpperCase().includes('EXPLORAR');
    const isEvidence = block.title?.toUpperCase().includes('TRILHA');
    const isChanged  = block.title?.toUpperCase().includes('MUDOU');

    html += `<div class="report-section${isChanged ? ' section-changed' : ''}">`;

    if (block.title) {
      html += `<div class="report-section-header">
        <div class="section-accent ${color}"></div>
        <div class="section-title">${escHtml(block.title)}</div>
      </div>`;
    }

    html += `<div class="report-section-body">`;

    if (isExplore && block.body) {
      html += renderExploreCards(block.body);
    } else if (isEvidence && block.body) {
      html += renderEvidenceTrail(block.body);
    } else if (block.body) {
      html += parseBodyToHtml(block.body);
    }

    if (isLast && !final) {
      html += '<span class="cursor-blink" style="display:inline-block;"></span>';
    }

    html += `</div></div>`;
  });

  reportContent.innerHTML = html;

  // Make citation numbers [N] clickable using sourcesMap
  if (sourcesMap.length) {
    reportContent.querySelectorAll('p, li').forEach(el => {
      el.innerHTML = el.innerHTML.replace(/\[(\d+)\]/g, (match, n) => {
        const idx = parseInt(n) - 1;
        const src = sourcesMap[idx];
        if (src && src.url) {
          return `<a href="${escHtml(src.url)}" target="_blank" rel="noopener" class="citation" title="${escHtml(src.title)}">[${n}]</a>`;
        }
        return match;
      });
    });
  }

  if (!final) {
    const wrapper = document.getElementById('report-wrapper');
    wrapper.scrollTop = wrapper.scrollHeight;
  }
}

// Render evidence trail with clickable links
function renderEvidenceTrail(body) {
  const lines = body.split('\n').filter(l => l.trim());
  if (!lines.length) return parseBodyToHtml(body);

  let html = '<div class="evidence-list">';
  lines.forEach(line => {
    // Format: - **[N]** title — claim — URL
    // or plain list items
    const citMatch = line.match(/^-\s+\*\*\[(\d+)\]\*\*\s+(.+?)(?:\s+—\s+(.+?))?(?:\s+—\s+(https?:\/\/\S+))?$/);
    if (citMatch) {
      const num   = citMatch[1];
      const title = citMatch[2] || '';
      const claim = citMatch[3] || '';
      const url   = citMatch[4] || (sourcesMap[parseInt(num)-1]?.url || '');
      html += `<div class="evidence-item">
        <span class="evidence-num">[${num}]</span>
        <div class="evidence-body">
          ${url ? `<a href="${escHtml(url)}" target="_blank" rel="noopener" class="evidence-title">${escHtml(title)}</a>` : `<span class="evidence-title">${escHtml(title)}</span>`}
          ${claim ? `<span class="evidence-claim">— ${escHtml(claim)}</span>` : ''}
        </div>
      </div>`;
    } else {
      // Fallback: try to linkify any URL in the line
      const text = line.replace(/^-\s+/, '');
      const withLinks = escHtml(text).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener" class="evidence-link">$1</a>');
      html += `<div class="evidence-item evidence-fallback"><span class="evidence-body">${withLinks}</span></div>`;
    }
  });
  html += '</div>';
  return html;
}

// Convert markdown body text to HTML
function parseBodyToHtml(body) {
  const lines = body.split('\n');
  let html   = '';
  let inList = false;

  lines.forEach(rawLine => {
    const line = rawLine.trim();
    if (!line) {
      if (inList) { html += '</ul>'; inList = false; }
      return;
    }

    if (line.startsWith('- ')) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inlineMarkdown(line.slice(2))}</li>`;
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<p>${inlineMarkdown(line)}</p>`;
    }
  });

  if (inList) html += '</ul>';
  return html;
}

// Inline markdown: **bold**, `code`
function inlineMarkdown(text) {
  return escHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
}

// Render "Explorar Também" section as clickable cards
function renderExploreCards(body) {
  const lines = body.split('\n').filter(l => l.trim().startsWith('- '));
  if (!lines.length) return parseBodyToHtml(body);

  let html = '<div class="explore-cards">';
  lines.forEach(line => {
    const match = line.match(/^-\s+\*\*(.+?)\*\*\s*[—–-]\s*(.+)$/);
    if (match) {
      const name = match[1].trim();
      const desc = match[2].trim();
      html += `
        <div class="explore-card" onclick="exploreItem('${escAttr(name)}')">
          <div class="explore-card-name">${escHtml(name)}</div>
          <div class="explore-card-desc">${escHtml(desc)}</div>
          <div class="explore-card-arrow">→</div>
        </div>`;
    } else {
      const text  = line.replace(/^-\s+/, '').replace(/\*\*/g, '');
      const parts = text.split(/[—–-]/);
      const name  = parts[0].trim();
      const desc  = parts.slice(1).join('-').trim();
      html += `
        <div class="explore-card" onclick="exploreItem('${escAttr(name)}')">
          <div class="explore-card-name">${escHtml(name)}</div>
          <div class="explore-card-desc">${escHtml(desc || '')}</div>
          <div class="explore-card-arrow">→</div>
        </div>`;
    }
  });
  html += '</div>';
  return html;
}

window.exploreItem = function(name) {
  if (currentMode === 'equity') {
    $('ticker').value = name;
    $('thesis').value = '';
    $('btn-equity').click();
  } else {
    $('startup-name').value = name;
    $('startup-thesis').value = '';
    $('btn-startup').click();
  }
};

// ── Market data bar ───────────────────────────────────────
function renderMarketDataBar(d) {
  const existing = document.getElementById('market-data-bar');
  if (existing) existing.remove();

  if (!d || !d.price) return;

  const pct = d.change_pct || '';
  const isPos = pct.startsWith('-') ? false : true;
  const changeClass = pct ? (isPos ? 'md-pos' : 'md-neg') : '';

  const pills = [
    d.pe_trailing ? `P/L ${d.pe_trailing}` : null,
    d.ev_ebitda   ? `EV/EBITDA ${d.ev_ebitda}` : null,
    d.pb          ? `P/VP ${d.pb}` : null,
    d.div_yield   ? `DY ${d.div_yield}` : null,
    d.market_cap  ? `Mktcap ${d.market_cap}` : null,
  ].filter(Boolean);

  const bar = document.createElement('div');
  bar.id = 'market-data-bar';
  bar.className = 'market-data-bar';
  bar.innerHTML = `
    <span class="md-ticker">${escHtml(d.ticker)}</span>
    <span class="md-price">${escHtml(d.price)}</span>
    ${pct ? `<span class="md-change ${changeClass}">${isPos ? '+' : ''}${escHtml(pct)}</span>` : ''}
    <span class="md-sep">|</span>
    ${pills.map(p => `<span class="md-pill">${escHtml(p)}</span>`).join('')}
    ${d.week_52_high && d.week_52_low ? `<span class="md-range">52w ${escHtml(d.week_52_low)}–${escHtml(d.week_52_high)}</span>` : ''}
  `;

  // Insert after verdict-bar
  verdictBar.insertAdjacentElement('afterend', bar);
}

// ── Verdict detection ─────────────────────────────────────
const VERDICTS = {
  green: ['TESE MANTIDA', 'INVESTIR', 'COMPRAR'],
  amber: ['TESE ALTERADA', 'MONITORAR', 'MANTER'],
  red:   ['TESE INVALIDADA', 'PASSAR', 'REDUZIR', 'VENDER'],
};

function detectVerdict(text) {
  const upper = text.toUpperCase();
  for (const [color, keywords] of Object.entries(VERDICTS)) {
    for (const kw of keywords) {
      if (upper.includes(`**${kw}**`) || upper.includes(`**[${kw}]**`)) {
        verdictTag.textContent = kw;
        verdictTag.className   = `verdict-tag ${color}`;
        const confMatch = text.match(/Confiança:\s*\*?\*?([A-ZÁÉÍÓÚÃÕ]+)\*?\*?/i);
        if (confMatch) verdictMeta.innerHTML = `Confiança: <strong>${confMatch[1]}</strong>`;
        return;
      }
    }
  }
}

// ── Queries ───────────────────────────────────────────────
let queryEls = [];

function renderQueries(queries, type) {
  if (type === 'initial') {
    queriesList.innerHTML = '';
    queryEls = [];
  } else {
    // Add separator for follow-up queries
    const sep = document.createElement('div');
    sep.className = 'query-separator';
    sep.textContent = '↳ follow-up';
    queriesList.appendChild(sep);
  }

  queries.forEach((q, i) => {
    const el = document.createElement('div');
    el.className = type === 'followup' ? 'query-item followup' : 'query-item';
    el.textContent = `${type === 'followup' ? '↳' : (queryEls.length + 1) + '.'} ${q.length > 46 ? q.slice(0, 46) + '…' : q}`;
    queriesList.appendChild(el);
    if (type === 'initial') queryEls.push(el);
  });
}

function setStatus(text, state) {
  statusText.textContent = text;
  statusDot.className    = `status-dot ${state}`;
  const match = text.match(/\[(\d+)\//);
  if (match) {
    const idx = parseInt(match[1]) - 1;
    queryEls.forEach((el, i) => {
      el.classList.remove('active');
      if (i < idx)   el.classList.add('done');
      if (i === idx) el.classList.add('active');
    });
  }
}

function markQueriesDone() {
  queryEls.forEach(el => { el.classList.remove('active'); el.classList.add('done'); });
  queriesList.querySelectorAll('.query-item.followup').forEach(el => el.classList.add('done'));
}

// ── Utils ─────────────────────────────────────────────────
function resetOutput() {
  reportBuffer = '';
  sourcesMap   = [];
  reportContent.innerHTML = '';
  reportWrapper.classList.remove('visible');
  verdictBar.classList.remove('visible');
  queriesList.innerHTML = '';
  statusPanel.classList.remove('visible');
  queryEls = [];
  emptyState.style.display = 'flex';
  if (renderFrame) { cancelAnimationFrame(renderFrame); renderFrame = null; }
  const mdBar = document.getElementById('market-data-bar');
  if (mdBar) mdBar.remove();
}

function setBtnState(disabled) {
  $('btn-equity').disabled  = disabled;
  $('btn-startup').disabled = disabled;
}

function highlight(id) {
  const el = $(id);
  el.style.borderColor = 'var(--red)';
  el.focus();
  setTimeout(() => { el.style.borderColor = ''; }, 1500);
}

function enc(s)     { return encodeURIComponent(s); }
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function escAttr(s) { return String(s).replace(/'/g,"\\'"); }

// ── HISTORY (localStorage) ────────────────────────────────
const HISTORY_KEY = 'cortiq_history_v1';
const MAX_HISTORY  = 20;

function historyLoad() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
  catch { return []; }
}

function historySave(entry) {
  const list = historyLoad().filter(
    e => !(e.key === entry.key && e.mode === entry.mode)
  );
  list.unshift(entry);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)));
  renderHistoryPanel();
}

function historyFind(mode, key) {
  return historyLoad().find(e => e.mode === mode && e.key.toLowerCase() === key.toLowerCase());
}

function historyFormatDate(iso) {
  const d = new Date(iso);
  const now = new Date();
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays === 0) return 'Hoje';
  if (diffDays === 1) return 'Ontem';
  if (diffDays < 7)  return `${diffDays}d atrás`;
  return d.toLocaleDateString('pt-BR', { day:'2-digit', month:'2-digit' });
}

function renderHistoryPanel() {
  const list  = historyLoad();
  const panel = $('history-panel');
  const listEl = $('history-list');

  if (!list.length) { panel.classList.remove('visible'); return; }
  panel.classList.add('visible');

  listEl.innerHTML = list.map(e => `
    <div class="history-item" onclick="historyOpen('${escAttr(e.key)}','${e.mode}')">
      <div class="history-item-badge ${e.verdictColor}">${e.verdict}</div>
      <div class="history-item-name">${escHtml(e.key)}</div>
      <div class="history-item-date">${historyFormatDate(e.date)}</div>
    </div>
  `).join('');
}

window.historyOpen = function(key, mode) {
  const entry = historyFind(mode, key);
  if (!entry) return;

  if (mode !== currentMode) {
    document.querySelector(`.mode-tab[data-mode="${mode}"]`)?.click();
  }

  if (mode === 'equity') {
    $('ticker').value  = entry.key;
    $('thesis').value  = entry.thesis || '';
    $('mandate').value = entry.mandate || '';
  } else {
    $('startup-name').value   = entry.key;
    $('startup-url').value    = entry.url || '';
    $('startup-thesis').value = entry.thesis || '';
  }

  showCachedReport(entry);
};

function showCachedReport(entry) {
  emptyState.style.display = 'none';
  reportWrapper.classList.add('visible');
  verdictBar.classList.add('visible');
  verdictTag.textContent  = entry.verdict;
  verdictTag.className    = `verdict-tag ${entry.verdictColor}`;
  verdictMeta.innerHTML   = entry.confidence ? `Confiança: <strong>${entry.confidence}</strong>` : '';
  verdictTime.textContent = historyFormatDate(entry.date);
  reportBuffer            = entry.report;
  sourcesMap              = entry.sources || [];
  renderBlocks(entry.report, true);
  showCacheHint(entry);
}

function showCacheHint(entry) {
  const hint    = $('cache-hint');
  const text    = $('cache-hint-text');
  const btnView = $('btn-view-cache');
  const btnRun  = $('btn-rerun');

  const dateStr = new Date(entry.date).toLocaleDateString('pt-BR', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit'
  });
  text.textContent = `Análise de ${dateStr}`;
  hint.style.display = 'flex';

  btnView.onclick = () => showCachedReport(entry);
  btnRun.onclick  = () => {
    hint.style.display = 'none';
    if (entry.mode === 'equity') $('btn-equity').click();
    else $('btn-startup').click();
  };
}

function historySaveCompleted(mode, key, report, verdict, verdictColor, confidence, extras) {
  historySave({
    mode, key, report, verdict, verdictColor, confidence,
    sources: sourcesMap,
    date: new Date().toISOString(),
    ...extras,
  });
}

function bindHistoryHints() {
  function check(inputId, mode, keyFn) {
    const el = $(inputId);
    if (!el) return;
    el.addEventListener('input', () => {
      const key  = keyFn().trim();
      const hint = $('cache-hint');
      if (!key) { hint.style.display = 'none'; return; }
      const found = historyFind(mode, key);
      if (found) {
        const dateStr = new Date(found.date).toLocaleDateString('pt-BR', {
          day: '2-digit', month: '2-digit', year: 'numeric',
        });
        $('cache-hint-text').textContent = `Análise anterior: ${dateStr} (${found.verdict})`;
        hint.style.display = 'flex';
        $('btn-view-cache').onclick = () => showCachedReport(found);
        $('btn-rerun').onclick = () => {
          hint.style.display = 'none';
          if (mode === 'equity') $('btn-equity').click();
          else $('btn-startup').click();
        };
      } else {
        hint.style.display = 'none';
      }
    });
  }

  check('ticker',       'equity',  () => $('ticker').value.toUpperCase());
  check('startup-name', 'startup', () => $('startup-name').value);
}

// ── Init ──────────────────────────────────────────────────
(function init() {
  renderHistoryPanel();
  bindHistoryHints();

  $('btn-clear-history').addEventListener('click', () => {
    localStorage.removeItem(HISTORY_KEY);
    renderHistoryPanel();
    $('cache-hint').style.display = 'none';
  });

  // Auto-trigger from URL params (coming from portfolio page)
  const params = new URLSearchParams(window.location.search);
  const tickerParam  = params.get('ticker');
  const startupParam = params.get('startup');

  if (tickerParam) {
    $('ticker').value = tickerParam.toUpperCase();
    setTimeout(() => $('btn-equity').click(), 100);
  } else if (startupParam) {
    // Switch to startup mode
    document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
    document.querySelector('[data-mode="startup"]').classList.add('active');
    equityForm.style.display = 'none';
    startupForm.style.display = 'block';
    currentMode = 'startup';
    $('startup-name').value = startupParam;
    const urlParam = params.get('url');
    if (urlParam) $('startup-url').value = urlParam;
    setTimeout(() => $('btn-startup').click(), 100);
  }
})();
