let theme = localStorage.getItem('openvidia-theme') || 'dark'
let keys = []
let activeModel = ''
let allModels = []
let presets = []
let _logEntries = 0
let modelFilter = 'popular'
let keyFilter = 'all'
let _lastKeyStats = null

/* ── Popular models shortcut ─────────────────── */
const POPULAR_MODELS = new Set([
  'deepseek-ai/deepseek-v4-pro',
  'deepseek-ai/deepseek-v4-flash',
  'meta/llama-4.1-maverick-8b-instruct',
  'meta/llama-3.3-70b-instruct',
  'mistralai/mistral-large-2411',
  'microsoft/phi-4-mini-instruct',
  'google/gemma-3-27b-it',
  'nvidia/llama-3.1-nemotron-70b-instruct',
  'nvidia/nemotron-4-340b-reward',
  'minimaxai/minimax-m3',
  'z-ai/glm-5.2',
  'moonshotai/kimi-k2.6',
  '01-ai/yi-large',
  'bytedance/seed-oss-36b-instruct',
  'ai21labs/jamba-1.5-large-instruct',
  'openai/gpt-4o',
  'openai/gpt-4o-mini',
  'google/gemma-3-12b-it',
  'mistralai/mixtral-8x22b-instruct',
])

const $ = id => document.getElementById(id)

/* ── API helper ──────────────────────────────── */
async function api(method, path, body) {
  const opts = { method, headers: {} }
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body) }
  const r = await fetch(path, opts)
  if (!r.ok) { let t; try { t = await r.text() } catch { t = r.statusText }; throw new Error(t) }
  return r.json()
}

/* ── Toast ──────────────────────────────────── */
function toast(msg, level = 'info') {
  const icons = { ok: '✓', error: '✕', warn: '⚠', info: 'ℹ' }
  const el = document.createElement('div')
  el.className = 'toast'
  el.innerHTML = `<span class="toast-icon ${level}">${icons[level] || icons.info}</span><span>${msg}</span>`
  $('toast-container').appendChild(el)
  setTimeout(() => { el.classList.add('toast-out'); setTimeout(() => el.remove(), 260) }, 3500)
}

/* ── Theme ──────────────────────────────────── */
function applyTheme(t) {
  theme = t
  document.documentElement.setAttribute('data-theme', t)
  $('themeToggle').textContent = t === 'dark' ? '🌙' : '☀️'
  localStorage.setItem('openvidia-theme', t)
}
$('themeToggle').addEventListener('click', () => applyTheme(theme === 'dark' ? 'light' : 'dark'))
applyTheme(theme)

/* ── Bento Dropdowns ────────────────────────── */
document.querySelectorAll('[data-toggle]').forEach(hd => {
  hd.addEventListener('click', () => hd.closest('[data-dropdown]').classList.toggle('open'))
})

/* ── Running State ──────────────────────────── */
async function updateRunningState() {
  try {
    const st = await api('GET', '/api/status')
    const running = st.running
    $('statusText').textContent = running ? 'Running' : 'Stopped'
    $('statusDot').querySelector('.pulse-dot').className = `pulse-dot ${running ? 'running' : ''}`
    $('stopBtn').style.display = running ? '' : 'none'
    $('startBtn').style.display = running ? 'none' : ''
    $('hKeyCount').textContent = `${st.keys || 0} keys`
  } catch (_) {
    $('statusText').textContent = 'Offline'
    $('statusDot').querySelector('.pulse-dot').className = 'pulse-dot'
  }
}

$('stopBtn').addEventListener('click', async () => { try { await api('POST', '/api/stop'); toast('Proxy stopped', 'warn') } catch (_) {} await updateRunningState() })
$('startBtn').addEventListener('click', async () => { try { await api('POST', '/api/start'); toast('Proxy started', 'ok') } catch (_) {} await updateRunningState() })

$('restartBtn').addEventListener('click', async () => {
  $('restartBtn').textContent = '⟳…'
  try { await api('POST', '/api/restart'); toast('Restarting…', 'info') } catch (_) { toast('Restart failed', 'error') }
  $('restartBtn').textContent = '⟳'
})

/* ── Stats polling ───────────────────────────── */
$('portDisplay').textContent = '1919'
setInterval(async () => {
  try {
    const s = await api('GET', '/api/stats')
    $('statReqs').textContent = s.requests
    $('statOk').textContent = s.success
    $('statRots').textContent = s.rotations
    const cd = s.cooldowns || 0
    $('statCooldowns').textContent = cd
    $('cdMini').classList.toggle('warn', cd > 0)
    $('hRpm').textContent = `${s.total_rpm || 0} RPM`
    await updateRunningState()
  } catch (_) {}
}, 2000)

/* ── Presets ────────────────────────────────── */
async function loadPresets() {
  try { const r = await api('GET', '/api/presets'); presets = r.presets || [] } catch (_) { presets = [] }
  renderPresets()
}
async function savePresets() { try { await api('POST', '/api/presets', { presets }) } catch (_) {} renderPresets() }

function labelForModel(id) { const m = allModels.find(x => x.id === id); return m ? shortLabel(m.id) : id }
function shortLabel(id) {
  const known = { 'z-ai/glm-5.2': 'GLM 5.2', 'deepseek-ai/deepseek-v4-pro': 'DeepSeek V4 Pro', 'minimaxai/minimax-m3': 'MiniMax M3' }
  if (known[id]) return known[id]
  return id.split('/').pop()
}

function renderPresets() {
  const grid = $('quickSwitch')
  $('presetCount').textContent = presets.length
  grid.innerHTML = ''
  presets.map(id => ({ id, label: labelForModel(id) })).forEach(p => {
    const btn = document.createElement('button')
    btn.textContent = p.label
    btn.className = p.id === activeModel ? 'active' : ''
    btn.onclick = () => setModel(p.id)
    const rm = document.createElement('span')
    rm.className = 'preset-rm'; rm.textContent = '×'; rm.title = 'Remove'
    rm.onclick = e => { e.stopPropagation(); presets = presets.filter(x => x !== p.id); savePresets() }
    btn.appendChild(rm)
    grid.appendChild(btn)
  })
}

async function setModel(id) {
  try {
    await fetch('/api/model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: id }) })
    activeModel = id
    $('activeModelDisplay').textContent = id || 'none'
    renderPresets(); renderModelList(); renderUsage()
    toast(`Model: ${id || 'none'}`, 'ok')
  } catch (_) {}
}

async function loadModel() {
  try { const r = await api('GET', '/api/model'); activeModel = r.model || ''; $('activeModelDisplay').textContent = activeModel || 'none' } catch (_) {}
}

/* ── Models browser ──────────────────────────── */
const FILTERS = ['popular', 'all']
function getFilteredModels() {
  let list = allModels
  if (modelFilter === 'popular') list = allModels.filter(m => POPULAR_MODELS.has(m.id))
  const q = ($('modelSearch').value || '').toLowerCase()
  if (q) list = list.filter(m => m.id.toLowerCase().includes(q))
  return list
}

function renderFilters() {
  const c = $('modelFilters'); c.innerHTML = ''
  FILTERS.forEach(f => {
    const b = document.createElement('button')
    b.className = `browser-filter ${f === modelFilter ? 'active' : ''}`
    b.textContent = f === 'popular' ? '★ Popular' : 'All'
    b.onclick = () => { modelFilter = f; renderFilters(); renderModelList() }
    c.appendChild(b)
  })
}

async function fetchModels() {
  try {
    const r = await fetch('/v1/models'); const d = await r.json()
    allModels = (d.data || d.models || []).map(m => ({ id: m.id, owned_by: m.owned_by || '' }))
    $('modelCount').textContent = allModels.length
    renderFilters(); renderModelList(); renderUsage()
  } catch (_) { $('modelList').innerHTML = '<div class="browser-empty">Failed to load</div>' }
}

async function testModel(id, btn) {
  btn.className = 'browser-test testing'; btn.textContent = '…'
  const r = document.createElement('div'); r.className = 'test-result'
  btn.parentElement.parentElement.after(r)
  try {
    const res = await api('POST', '/api/test-model', { model: id })
    if (res.ok) { r.className = 'test-result ok'; r.textContent = `✓ ${(res.response || '').slice(0, 80)}`; btn.className = 'browser-test done'; btn.textContent = '✓' }
    else { r.className = 'test-result fail'; r.textContent = `✗ ${res.detail || res.error || 'unknown'}`.slice(0, 120); btn.className = 'browser-test fail'; btn.textContent = '✗' }
  } catch (e) { r.className = 'test-result fail'; r.textContent = `✗ ${e.message.slice(0, 80)}`; btn.className = 'browser-test fail'; btn.textContent = '✗' }
  setTimeout(() => r.remove(), 8000)
  setTimeout(() => { btn.className = 'browser-test'; btn.textContent = '▶' }, 5000)
}

function renderModelList() {
  const f = getFilteredModels()
  $('modelCount').textContent = `${f.length} / ${allModels.length}`
  if (!f.length) { $('modelList').innerHTML = '<div class="browser-empty">No models found</div>'; return }
  $('modelList').innerHTML = ''
  f.forEach(m => {
    const item = document.createElement('div')
    item.className = `browser-item ${m.id === activeModel ? 'active' : ''}`
    item.innerHTML = `<span class="browser-name">${m.id}</span>${m.owned_by ? `<span class="browser-owner">${m.owned_by}</span>` : ''}`
    const acts = document.createElement('div'); acts.className = 'browser-acts'
    const test = document.createElement('button'); test.className = 'browser-test'; test.textContent = '▶'; test.title = 'Test'
    test.onclick = e => { e.stopPropagation(); testModel(m.id, test) }
    acts.appendChild(test)
    if (!presets.includes(m.id)) {
      const add = document.createElement('button'); add.className = 'browser-add'
      add.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
      add.title = 'Add to presets'
      add.onclick = e => { e.stopPropagation(); presets.push(m.id); savePresets(); toast(`Added ${shortLabel(m.id)}`, 'ok') }
      acts.appendChild(add)
    } else {
      const chk = document.createElement('span'); chk.className = 'browser-check'; chk.textContent = '✓'; acts.appendChild(chk)
    }
    item.appendChild(acts)
    item.onclick = () => setModel(m.id)
    $('modelList').appendChild(item)
  })
}
$('modelSearch').addEventListener('input', renderModelList)

/* ── Key management: CRUD + filters ──────────── */
function maskKey(k) { return k.length <= 14 ? k : `${k.slice(0, 8)}…${k.slice(-4)}` }
function copyToClipboard(t) { navigator.clipboard.writeText(t).then(() => toast('Key copied', 'ok')).catch(() => {}) }
function timeAgo(ts) { if (!ts) return ''; const s = Math.floor(Date.now() / 1000 - ts); if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)}m`; return `${Math.floor(s / 3600)}h` }

function keyStatus(k, i, stats) {
  const s = stats ? stats[String(i)] : null
  if (s && s.cooldown > 0) return 'cooldown'
  if (s && s.success > 0) return 'fresh'
  if (s && s.requests > 0) return 'stale'
  return 'idle'
}

function updateFilterCounts(stats) {
  if (!stats) return
  const sm = stats.key_stats || {}
  let active = 0, cooldown = 0, fresh = 0, idle = 0
  keys.forEach((k, i) => {
    const st = keyStatus(k, i, sm)
    if (st === 'cooldown') cooldown++
    else if (st === 'fresh') fresh++
    else if (st === 'stale') fresh++
    else idle++
    if (i === stats.active_index) active++
  })
  $('fAll').textContent = keys.length
  $('fActive').textContent = active
  $('fCooldown').textContent = cooldown
  $('fFresh').textContent = fresh
  $('fIdle').textContent = idle
}

function renderKeys(data) {
  _lastKeyStats = data
  const list = $('keysList')
  list.innerHTML = ''
  $('keyCount').textContent = String(keys.length)
  $('keysEmpty').classList.toggle('hidden', keys.length > 0)
  const ai = data ? data.active_index : -1
  const sm = data ? data.key_stats || {} : {}
  updateFilterCounts(data)
  keys.forEach((k, i) => {
    const s = sm[String(i)]
    const status = keyStatus(k, i, sm)
    if (keyFilter !== 'all' && keyFilter !== status && !(keyFilter === 'active' && i === ai)) return
    const isCd = s && s.cooldown > 0
    const row = document.createElement('div')
    row.className = `key-row ${i === ai ? 'active' : ''}`
    const dot = document.createElement('span')
    let dc = 'stale'
    if (isCd) dc = 'dead'
    else if (s && s.success > 0) dc = 'fresh'
    else if (!s || !s.requests) dc = 'unused'
    dot.className = `key-dot key-${dc}`
    row.appendChild(dot)
    const val = document.createElement('span')
    val.className = 'key-val'; val.textContent = maskKey(k); val.title = k
    val.onclick = () => copyToClipboard(k)
    row.appendChild(val)
    const acts = document.createElement('div'); acts.className = 'key-acts'
    if (i === ai) { const b = document.createElement('span'); b.className = 'key-badge'; b.textContent = isCd ? '⏳' : 'active'; acts.appendChild(b) }
    const info = document.createElement('span'); info.className = 'key-info'
    if (isCd) { info.textContent = `⏳ ${Math.ceil(s.cooldown)}s`; info.title = s.cooldown_reason || '' }
    else if (s && s.requests > 0) { info.textContent = `${s.success}✓${s.failed > 0 ? ' ' + s.failed + '✗' : ''} ${timeAgo(s.last_used)}`; info.title = s.last_error || '' }
    else { info.textContent = 'idle' }
    acts.appendChild(info)
    const cp = document.createElement('button'); cp.className = 'key-act'; cp.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; cp.title = 'Copy'; cp.onclick = () => copyToClipboard(k)
    acts.appendChild(cp)
    const del = document.createElement('button'); del.className = 'key-act danger'; del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'; del.title = 'Remove'; del.onclick = () => {
      keys = keys.filter(x => x !== k)
      persistKeys(); toast('Key removed', 'warn')
    }
    acts.appendChild(del)
    row.appendChild(acts)
    list.appendChild(row)
  })
}

async function pollKeyStats() {
  try {
    const data = await api('GET', '/api/keys/stats')
    renderKeys(data)
  } catch (_) {}
}

async function persistKeys() {
  try { await api('POST', '/api/keys', { keys }) } catch (e) { toast(`Save failed: ${e.message}`, 'error') }
  renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
}

/* ── Key filter buttons ──────────────────────── */
document.querySelectorAll('.key-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    keyFilter = btn.dataset.filter
    document.querySelectorAll('.key-filter').forEach(b => b.classList.toggle('active', b === btn))
    renderKeys(_lastKeyStats)
  })
})

/* ── Add key form ────────────────────────────── */
$('addKeyBtn').addEventListener('click', () => { $('keyAddForm').classList.remove('hidden'); $('newKeyInput').value = ''; $('newKeyInput').focus() })
$('cancelAddKeyBtn').addEventListener('click', () => $('keyAddForm').classList.add('hidden'))
$('confirmAddKeyBtn').addEventListener('click', async () => {
  const v = $('newKeyInput').value.trim()
  if (!v) return toast('Enter a key', 'warn')
  if (keys.includes(v)) return toast('Key already exists', 'warn')
  keys.push(v)
  $('keyAddForm').classList.add('hidden')
  await persistKeys()
  toast('Key added', 'ok')
})
$('newKeyInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('confirmAddKeyBtn').click() })

/* ── Log ────────────────────────────────────── */
$('clearLogBtn').addEventListener('click', () => { $('logArea').innerHTML = '<div class="log-empty">Cleared</div>'; _logEntries = 0; $('logCount').textContent = '0' })

function appendLog(level, msg) {
  const t = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const el = document.createElement('div')
  el.className = `log-entry ${level}`
  const time = document.createElement('span'); time.className = 'log-time'; time.textContent = t
  const text = document.createElement('span'); text.className = 'log-msg'; text.textContent = msg
  el.appendChild(time); el.appendChild(text)
  const e = $('logArea').querySelector('.log-empty')
  if (e) e.remove()
  $('logArea').appendChild(el)
  $('logArea').scrollTop = $('logArea').scrollHeight
  _logEntries++; $('logCount').textContent = _logEntries
  $('logDot').className = 'log-dot active'
  clearTimeout($('logDot')._timer)
  $('logDot')._timer = setTimeout(() => { $('logDot').className = 'log-dot' }, 2000)
}

new EventSource('/api/logs/stream').onmessage = e => {
  try {
    const { msg } = JSON.parse(e.data)
    let level = 'info'
    if (/cooldown|429|rate.limit|⏳/.test(msg)) level = 'cooldown'
    else if (/error|Error|✗|failed/.test(msg)) level = 'error'
    else if (/OK|✔|✓|revived/.test(msg)) level = 'ok'
    appendLog(level, msg)
  } catch (_) {}
}

/* ── News ────────────────────────────────────── */
async function loadNews(refresh = false) {
  try {
    const d = await api('GET', `/api/news${refresh ? '?refresh=true' : ''}`)
    const news = d.news || []
    $('newsCount').textContent = news.length
    const list = $('newsList')
    if (!news.length) { list.innerHTML = '<div class="browser-empty">No updates</div>'; return }
    list.innerHTML = ''
    news.forEach(n => {
      const item = document.createElement('div'); item.className = 'news-item'
      const a = document.createElement('a'); a.href = n.url; a.target = '_blank'; a.textContent = n.title
      item.appendChild(a)
      if (n.excerpt) { const ex = document.createElement('div'); ex.className = 'news-excerpt'; ex.textContent = n.excerpt.replace(/<[^>]+>/g, '').slice(0, 200); item.appendChild(ex) }
      const m = document.createElement('div'); m.className = 'news-meta'; m.textContent = [n.author, n.time.slice(0, 10)].filter(Boolean).join(' · '); item.appendChild(m)
      list.appendChild(item)
    })
  } catch (_) { $('newsList').innerHTML = '<div class="browser-empty">Failed to load</div>' }
}

/* ── Refresh news button ─────────────────────── */
if ($('refreshNewsBtn')) {
  $('refreshNewsBtn').addEventListener('click', async () => {
    $('refreshNewsBtn').disabled = true
    $('refreshNewsBtn').textContent = '…'
    try { await loadNews(true); toast('News refreshed', 'ok') } catch (_) { toast('Refresh failed', 'error') }
    $('refreshNewsBtn').disabled = false
    $('refreshNewsBtn').textContent = '↻'
  })
}

/* ── CLI Setup tab (opencode / Codex / Claude Code / Grok) ──────── */
let currentCli = 'opencode'
const CLI_TABS = [
  { id: 'opencode', label: 'opencode', icon: '⚡' },
  { id: 'codex',    label: 'Codex',    icon: '⬡' },
  { id: 'claude',   label: 'Claude',   icon: '✦' },
  { id: 'grok',     label: 'Grok',     icon: '✸' },
]

function renderCliTabs() {
  const c = $('cliTabs')
  if (!c) return
  c.innerHTML = ''
  CLI_TABS.forEach(t => {
    const b = document.createElement('button')
    b.className = `cli-tab ${t.id === currentCli ? 'active' : ''}`
    b.innerHTML = `<span class="cli-tab-icon">${t.icon}</span>${t.label}`
    b.onclick = () => { currentCli = t.id; renderCliTabs(); renderUsage() }
    c.appendChild(b)
  })
}

function renderUsage() {
  const m = activeModel || 'openvidia'
  const examples = {
    opencode: {
      title: 'opencode',
      steps: [
        { label: 'Config', code: `# ~/.config/opencode/opencode.json
{
  "provider": "openvidia",
  "model": "${m}",
  "api_base": "http://localhost:1919/v1"
}` },
        { label: 'Run', code: `opencode` },
      ],
    },
    codex: {
      title: 'Codex CLI',
      steps: [
        { label: 'Env', code: `export OPENAI_API_KEY=ignored
export OPENAI_BASE_URL=http://localhost:1919/v1` },
        { label: 'Config', code: `# ~/.codex/config.toml
model = "${m}"
api_base_url = "http://localhost:1919/v1"` },
        { label: 'Run', code: `codex exec "explain this codebase"` },
      ],
    },
    claude: {
      title: 'Claude Code',
      steps: [
        { label: 'Env (temporary, this shell only)', code: `export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored` },
        { label: 'Run', code: `claude --model ${m}` },
        { label: 'Or one-shot', code: `ANTHROPIC_BASE_URL=http://localhost:1919 \\
  ANTHROPIC_API_KEY=ignored \\
  claude -p "explain this file"` },
      ],
    },
    grok: {
      title: 'Grok (xAI)',
      steps: [
        { label: 'config.toml', code: `# ~/.grok/config.toml
[provider.openvidia]
base_url = "http://localhost:1919/v1"
api_key = "ignored"

[model."${m}"]
provider = "openvidia"` },
        { label: 'Run', code: `grok -m ${m} "explain this codebase"` },
      ],
    },
  }

  const ex = examples[currentCli]
  if (!ex) return
  const container = $('usageCode') || $('cliSetup')
  if (!container) return
  const stepsHtml = ex.steps.map((s, i) => `
    <div class="cli-step">
      <div class="cli-step-label">${i + 1}. ${s.label}</div>
      <pre class="cli-step-code"><code>${s.code}</code></pre>
    </div>
  `).join('')
  container.innerHTML = `<div class="cli-setup-content"><h4>${ex.title}</h4>${stepsHtml}</div>`
}

/* ── Init ───────────────────────────────────── */
;(async () => {
  try {
    const d = await api('GET', '/api/keys'); keys = d.keys || []
    await loadModel()
    await loadPresets()
    await fetchModels()
    await loadNews()
    renderKeys()
    if (keys.length) toast(`${keys.length} keys loaded`, 'ok')
  } catch (_) {}
  await updateRunningState()
  renderCliTabs()
  renderUsage()
  setInterval(pollKeyStats, 2000)
})()
