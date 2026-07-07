/* ═══════════════════════════════════════════════
   OpenVidia — Frontend Controller
   Minimal · Manual Keys · Web UI
   ═══════════════════════════════════════════════ */

let statsInterval = null
let keyStatsInterval = null
let keys = []
let theme = localStorage.getItem('openvidia-theme') || 'dark'

/* ── DOM refs ────────────────────────────────── */
const $ = (id) => document.getElementById(id)

const statusText = $('statusText')
const portDisplay = $('portDisplay')
const statReqs = $('statReqs')
const statRots = $('statRots')
const statOk = $('statOk')

const logArea = $('logArea')
const logCount = $('logCount')
const logDot = $('logDot')
const clearLogBtn = $('clearLogBtn')

const keysList = $('keysList')
const keyCount = $('keyCount')
const keysEmpty = $('keysEmpty')
const addKeyBtn = $('addKeyBtn')
const keyAddForm = $('keyAddForm')
const newKeyInput = $('newKeyInput')
const confirmAddKeyBtn = $('confirmAddKeyBtn')
const cancelAddKeyBtn = $('cancelAddKeyBtn')

const quickSwitch = $('quickSwitch')
const modelStatusS = $('modelStatusS')
const usageCode = $('usageCode')
const themeDark = $('themeDark')
const themeLight = $('themeLight')
const modelBrowser = $('modelBrowser')
const modelList = $('modelList')
const modelSearch = $('modelSearch')
const modelCount = $('modelCount')

/* ── API helper ─────────────────────────────── */
async function api(method, path, body) {
  const opts = { method, headers: {} }
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body) }
  const r = await fetch(path, opts)
  if (!r.ok) { let t; try { t = await r.text() } catch { t = r.statusText }; throw new Error(t) }
  return r.json()
}

/* ── Toast system ───────────────────────────── */
function toast(msg, level = 'info') {
  const container = $('toast-container')
  const icons = { ok: '✓', error: '✕', warn: '⚠', info: 'ℹ' }
  const el = document.createElement('div')
  el.className = `toast`
  el.innerHTML = `<div class="toast-icon ${level}">${icons[level] || icons.info}</div><div class="toast-msg">${msg}</div>`
  container.appendChild(el)
  setTimeout(() => {
    el.classList.add('toast-out')
    setTimeout(() => el.remove(), 260)
  }, 3500)
}

/* ── Tabs ───────────────────────────────────── */
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => { t.classList.remove('active'); t.ariaSelected = 'false' })
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'))
    tab.classList.add('active'); tab.ariaSelected = 'true'
    const panel = $('tab-' + tab.dataset.tab)
    if (panel) panel.classList.add('active')
  })
})

/* ── Theme ───────────────────────────────────── */
function applyTheme(t) {
  theme = t
  document.documentElement.setAttribute('data-theme', t)
  localStorage.setItem('openvidia-theme', t)
  themeDark.classList.toggle('active', t === 'dark')
  themeLight.classList.toggle('active', t === 'light')
}

themeDark.addEventListener('click', () => applyTheme('dark'))
themeLight.addEventListener('click', () => applyTheme('light'))
applyTheme(theme)

/* ── Stats ──────────────────────────────────── */
statusText.textContent = 'Running'
portDisplay.textContent = '3940'

if (statsInterval) clearInterval(statsInterval)
statsInterval = setInterval(pollStats, 1000)

async function pollStats() {
  try {
    const s = await api('GET', '/api/stats')
    statReqs.textContent = s.requests
    statRots.textContent = s.rotations
    statOk.textContent = s.success
  } catch (_) {}
}

/* ── Model Presets ──────────────────────────── */
const PRESETS = [
  { id: '', label: 'Passthrough' },
  { id: 'z-ai/glm-5.2', label: 'GLM 5.2' },
  { id: 'deepseek-ai/deepseek-v4-pro', label: 'DeepSeek V4 Pro' },
  { id: 'minimaxai/minimax-m3', label: 'MiniMax M3' },
]

let activeModel = ''

function renderPresets() {
  if (!quickSwitch) return
  quickSwitch.innerHTML = ''
  PRESETS.forEach(p => {
    const btn = document.createElement('button')
    btn.textContent = p.label
    btn.className = p.id === activeModel ? 'active' : ''
    btn.addEventListener('click', () => setModel(p.id))
    quickSwitch.appendChild(btn)
  })
}

async function setModel(id) {
  try {
    const r = await fetch('/api/model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: id }) })
    if (r.ok) {
      activeModel = id
      modelStatusS.textContent = id || 'passthrough'
      renderPresets()
      toast(id ? `Model: ${id}` : 'Passthrough mode', 'ok')
    }
  } catch (_) {}
}

async function loadModel() {
  try {
    const r = await api('GET', '/api/model')
    activeModel = r.model || ''
    modelStatusS.textContent = activeModel || 'passthrough'
    renderPresets()
  } catch (_) {}
}

/* ── Model Browser ─────────────────────────── */
let allModels = []

async function fetchModels() {
  try {
    const r = await fetch('/v1/models')
    if (!r.ok) throw new Error(r.statusText)
    const d = await r.json()
    allModels = (d.data || []).map(m => ({ id: m.id, owned_by: m.owned_by || '' }))
    modelCount.textContent = allModels.length
    renderModelList()
  } catch (_) {
    modelList.innerHTML = '<div class="model-list-empty">Failed to load models</div>'
  }
}

function renderModelList() {
  const q = (modelSearch.value || '').toLowerCase()
  const filtered = q ? allModels.filter(m => m.id.toLowerCase().includes(q)) : allModels
  if (!filtered.length) {
    modelList.innerHTML = '<div class="model-list-empty">No models found</div>'
    return
  }
  modelList.innerHTML = ''
  filtered.forEach(m => {
    const item = document.createElement('div')
    item.className = `model-item ${m.id === activeModel ? 'active' : ''}`
    const name = document.createElement('span')
    name.className = 'model-item-name'
    name.textContent = m.id
    item.appendChild(name)
    if (m.owned_by) {
      const own = document.createElement('span')
      own.className = 'model-item-owner'
      own.textContent = m.owned_by
      item.appendChild(own)
    }
    item.addEventListener('click', () => setModel(m.id))
    modelList.appendChild(item)
  })
}

modelSearch.addEventListener('input', renderModelList)

/* ── Usage Example ──────────────────────────── */
const USAGE_EXAMPLES = {
  curl: `curl http://localhost:3940/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "z-ai/glm-5.2",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`,
  python: `from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3940/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="z-ai/glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)`,
  js: `const response = await fetch("http://localhost:3940/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "z-ai/glm-5.2",
    messages: [{ role: "user", content: "Hello!" }]
  })
})
const data = await response.json()
console.log(data.choices[0].message.content)`,
}

let currentUsageLang = 'curl'

document.querySelectorAll('.usage-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.usage-tab').forEach(t => t.classList.remove('active'))
    tab.classList.add('active')
    currentUsageLang = tab.dataset.usage
    renderUsageExample()
  })
})

function renderUsageExample() {
  usageCode.innerHTML = `<code>${USAGE_EXAMPLES[currentUsageLang]}</code>`
}

/* ── Key helpers ────────────────────────────── */
function maskKey(k) {
  return k.length <= 14 ? k : `${k.slice(0, 8)}…${k.slice(-4)}`
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => toast('Key copied to clipboard', 'ok')).catch(() => {})
}

function timeAgo(ts) {
  if (!ts) return ''
  const sec = Math.floor((Date.now() / 1000) - ts)
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const h = Math.floor(min / 60)
  return `${h}h ago`
}

/* ── Render keys ────────────────────────────── */
function renderKeys(keyStatsData) {
  keysList.innerHTML = ''
  if (!keys.length) {
    keysEmpty.style.display = 'flex'
    keyCount.textContent = '0'
    return
  }
  keysEmpty.style.display = 'none'
  keyCount.textContent = String(keys.length)

  const activeIdx = keyStatsData ? keyStatsData.active_index : -1
  const statsMap = keyStatsData ? keyStatsData.key_stats || {} : {}

  keys.forEach((k, i) => {
    const isActive = i === activeIdx
    const st = statsMap[String(i)]
    const freshness = st ? st.freshness : 'unused'
    const row = document.createElement('div')
    row.className = `key-row ${isActive ? 'key-active' : ''}`

    const left = document.createElement('div'); left.className = 'key-left'
    const indicator = document.createElement('span')
    indicator.className = `key-indicator key-${freshness}`
    indicator.title = freshness === 'fresh' ? 'Used recently' : freshness === 'stale' ? 'Not used recently' : 'Never used'
    left.appendChild(indicator)

    const val = document.createElement('span'); val.className = 'key-value'
    val.textContent = maskKey(k); val.title = k
    val.addEventListener('click', () => copyToClipboard(k))
    left.appendChild(val)
    row.appendChild(left)

    const group = document.createElement('div'); group.className = 'key-actions-group'
    if (isActive) {
      const activeBadge = document.createElement('span'); activeBadge.className = 'key-active-badge'
      activeBadge.textContent = 'active'
      group.appendChild(activeBadge)
    }
    if (st && st.requests > 0) {
      const usageInfo = document.createElement('span'); usageInfo.className = 'key-usage-info'
      usageInfo.textContent = `${st.success}✓ ${st.failed > 0 ? st.failed + '✗ ' : ''}${timeAgo(st.last_used)}`
      usageInfo.title = st.last_error || ''
      group.appendChild(usageInfo)
    }
    const copyBtn = document.createElement('button'); copyBtn.className = 'key-action-btn'
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    copyBtn.title = 'Copy key'; copyBtn.addEventListener('click', () => copyToClipboard(k))
    group.appendChild(copyBtn)
    const delBtn = document.createElement('button'); delBtn.className = 'key-action-btn danger'
    delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    delBtn.title = 'Remove key'; delBtn.addEventListener('click', () => { keys.splice(i, 1); persistKeys(); toast('Key removed', 'warn') })
    group.appendChild(delBtn)
    row.appendChild(group)
    keysList.appendChild(row)
  })
}

async function pollKeyStats() {
  try {
    const data = await api('GET', '/api/keys/stats')
    renderKeys(data)
  } catch (_) {}
}

async function persistKeys() {
  renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
  try { await api('POST', '/api/keys', { keys }) } catch (e) { toast(`Save failed: ${e.message}`, 'error') }
}

/* ── Key add form ──────────────────────────── */
addKeyBtn.addEventListener('click', () => {
  keyAddForm.classList.remove('hidden'); newKeyInput.value = ''; newKeyInput.focus()
})
cancelAddKeyBtn.addEventListener('click', () => keyAddForm.classList.add('hidden'))
confirmAddKeyBtn.addEventListener('click', async () => {
  const v = newKeyInput.value.trim()
  if (!v) { toast('Enter a key value', 'warn'); return }
  if (keys.includes(v)) { toast('Key already in list', 'warn'); return }
  keys.push(v)
  keyAddForm.classList.add('hidden')
  await persistKeys()
  toast('Key added', 'ok')
})
newKeyInput.addEventListener('keydown', e => { if (e.key === 'Enter') confirmAddKeyBtn.click() })

/* ── Log ────────────────────────────────────── */
let _logEntries = 0

function appendLog(level, msg) {
  const t = new Date().toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const el = document.createElement('div'); el.className = `log-entry ${level}`
  el.textContent = `${t}  ${msg}`
  const empty = logArea.querySelector('.log-empty')
  if (empty) empty.remove()
  logArea.appendChild(el)
  logArea.scrollTop = logArea.scrollHeight
  _logEntries++
  if (logCount) logCount.textContent = _logEntries
  if (logDot) {
    logDot.className = 'log-dot active'
    clearTimeout(logDot._timer)
    logDot._timer = setTimeout(() => { if (logDot) logDot.className = 'log-dot' }, 2000)
  }
}

clearLogBtn.addEventListener('click', () => {
  logArea.innerHTML = '<div class="log-empty">Cleared</div>'
  _logEntries = 0
  if (logCount) logCount.textContent = '0'
})

const evtSource = new EventSource('/api/logs/stream')
evtSource.onmessage = (e) => {
  try {
    const { msg } = JSON.parse(e.data)
    let level = 'info'
    if (/error|Error|✗/.test(msg)) level = 'error'
    else if (/OK|✔|✓/.test(msg)) level = 'ok'
    else if (/warn|⚠/.test(msg)) level = 'warn'
    appendLog(level, msg)
  } catch (_) {}
}

/* ── Init ───────────────────────────────────── */
;(async () => {
  renderKeys()
  renderUsageExample()
  try {
    const data = await api('GET', '/api/keys')
    keys = data.keys; renderKeys()
    if (keys.length) toast(`Loaded ${keys.length} keys`, 'ok')
  } catch (_) {}
  try {
    const st = await api('GET', '/api/status')
    toast(`Proxy running on :${st.port}`, 'ok')
  } catch (_) {}
  await loadModel()
  await fetchModels()

  if (keyStatsInterval) clearInterval(keyStatsInterval)
  keyStatsInterval = setInterval(pollKeyStats, 2000)
})()
