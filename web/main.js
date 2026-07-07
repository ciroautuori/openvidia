let theme = localStorage.getItem('openvidia-theme') || 'dark'
let keys = []
let activeModel = ''
let allModels = []
let presets = []
let _logEntries = 0
let statsInterval, keyStatsInterval

const $ = id => document.getElementById(id)

/* ── API ────────────────────────────────────── */
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

/* ── Theme ──────────────────────────────────── */
function applyTheme(t) {
  theme = t
  document.documentElement.setAttribute('data-theme', t)
  $('themeToggle').textContent = t === 'dark' ? '🌙' : '☀️'
  localStorage.setItem('openvidia-theme', t)
}
$('themeToggle').addEventListener('click', () => applyTheme(theme === 'dark' ? 'light' : 'dark'))
applyTheme(theme)

/* ── Stats ──────────────────────────────────── */
$('statusText').textContent = 'Running'
$('portDisplay').textContent = '3940'
statsInterval = setInterval(async () => {
  try {
    const s = await api('GET', '/api/stats')
    $('statReqs').textContent = s.requests
    $('statRots').textContent = s.rotations
    $('statOk').textContent = s.success
  } catch (_) {}
}, 1000)

/* ── Model Presets ──────────────────────────── */
async function loadPresets() {
  try {
    const r = await api('GET', '/api/presets')
    presets = r.presets || []
  } catch (_) { presets = [] }
  renderPresets()
}

async function savePresets() {
  try { await api('POST', '/api/presets', { presets }) } catch (_) {}
  renderPresets()
}

function renderPresets() {
  $('quickSwitch').innerHTML = ''
  const all = [{ id: '', label: 'Passthrough' }, ...presets.map(id => ({ id, label: labelForModel(id) }))]
  all.forEach(p => {
    const btn = document.createElement('button')
    btn.textContent = p.label
    btn.className = p.id === activeModel ? 'active' : ''
    btn.onclick = () => setModel(p.id)
    $('quickSwitch').appendChild(btn)
    if (p.id) {
      const rm = document.createElement('span')
      rm.className = 'preset-rm'
      rm.textContent = '×'
      rm.title = 'Remove preset'
      rm.onclick = e => { e.stopPropagation(); presets = presets.filter(x => x !== p.id); savePresets() }
      btn.appendChild(rm)
    }
  })
}

function labelForModel(id) {
  const m = allModels.find(x => x.id === id)
  return m ? shortLabel(m.id) : id
}

function shortLabel(id) {
  const known = { 'z-ai/glm-5.2': 'GLM 5.2', 'deepseek-ai/deepseek-v4-pro': 'DeepSeek V4 Pro', 'minimaxai/minimax-m3': 'MiniMax M3' }
  if (known[id]) return known[id]
  const parts = id.split('/')
  return parts[parts.length - 1]
}

async function setModel(id) {
  try {
    const r = await fetch('/api/model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: id }) })
    if (r.ok) {
      activeModel = id
      $('modelStatusS').textContent = id || 'passthrough'
      renderPresets()
      renderModelList()
      toast(id ? `Model: ${id}` : 'Passthrough mode', 'ok')
    }
  } catch (_) {}
}

async function loadModel() {
  try {
    const r = await api('GET', '/api/model')
    activeModel = r.model || ''
    $('modelStatusS').textContent = activeModel || 'passthrough'
    renderPresets()
  } catch (_) {}
}

/* ── Model Browser ──────────────────────────── */
async function fetchModels() {
  try {
    const r = await fetch('/v1/models')
    const d = await r.json()
    allModels = (d.data || []).map(m => ({ id: m.id, owned_by: m.owned_by || '' }))
    $('modelCount').textContent = allModels.length
    renderModelList()
  } catch (_) {
    $('modelList').innerHTML = '<div class="browser-empty">Failed to load</div>'
  }
}

function renderModelList() {
  const q = ($('modelSearch').value || '').toLowerCase()
  const f = q ? allModels.filter(m => m.id.toLowerCase().includes(q)) : allModels
  if (!f.length) { $('modelList').innerHTML = '<div class="browser-empty">No models found</div>'; return }
  $('modelList').innerHTML = ''
  f.forEach(m => {
    const item = document.createElement('div')
    item.className = `browser-item ${m.id === activeModel ? 'active' : ''}`
    const inPreset = presets.includes(m.id)
    item.innerHTML = `<span class="browser-name">${m.id}</span>${m.owned_by ? `<span class="browser-owner">${m.owned_by}</span>` : ''}`
    const acts = document.createElement('div')
    acts.className = 'browser-acts'
    if (!inPreset) {
      const addBtn = document.createElement('button')
      addBtn.className = 'browser-add'
      addBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
      addBtn.title = 'Add to presets'
      addBtn.onclick = e => { e.stopPropagation(); presets.push(m.id); savePresets(); toast(`Added ${shortLabel(m.id)}`, 'ok') }
      acts.appendChild(addBtn)
    } else {
      const chk = document.createElement('span')
      chk.className = 'browser-check'
      chk.textContent = '✓'
      acts.appendChild(chk)
    }
    item.appendChild(acts)
    item.onclick = () => setModel(m.id)
    $('modelList').appendChild(item)
  })
}
$('modelSearch').addEventListener('input', renderModelList)

/* ── Usage Example ──────────────────────────── */
const EXAMPLES = {
  curl: `curl http://localhost:3940/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{"model":"minimaxai/minimax-m3","messages":[{"role":"user","content":"Hello!"}]}'`,
  python: `from openai import OpenAI
client = OpenAI(base_url="http://localhost:3940/v1", api_key="ignored")
r = client.chat.completions.create(
    model="minimaxai/minimax-m3",
    messages=[{"role":"user","content":"Hello!"}]
)
print(r.choices[0].message.content)`,
  js: `const r = await fetch("http://localhost:3940/v1/chat/completions", {
  method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({model:"minimaxai/minimax-m3",
    messages:[{role:"user",content:"Hello!"}]})
})
const d = await r.json()
console.log(d.choices[0].message.content)`,
}
let currentLang = 'curl'
document.querySelectorAll('.usage-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.usage-tab').forEach(t => t.classList.remove('active'))
    tab.classList.add('active')
    currentLang = tab.dataset.usage
    $('usageCode').innerHTML = `<code>${EXAMPLES[currentLang]}</code>`
  })
})

/* ── Keys ───────────────────────────────────── */
function maskKey(k) { return k.length <= 14 ? k : `${k.slice(0, 8)}…${k.slice(-4)}` }
function copyToClipboard(t) { navigator.clipboard.writeText(t).then(() => toast('Key copied', 'ok')).catch(() => {}) }
function timeAgo(ts) {
  if (!ts) return ''
  const sec = Math.floor(Date.now() / 1000 - ts)
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec / 3600)}h`
}

function renderKeys(data) {
  const list = $('keysList')
  list.innerHTML = ''
  if (!keys.length) { $('keysEmpty').style.display = 'flex'; $('keyCount').textContent = '0'; return }
  $('keysEmpty').style.display = 'none'
  $('keyCount').textContent = String(keys.length)
  const ai = data ? data.active_index : -1
  const sm = data ? data.key_stats || {} : {}
  keys.forEach((k, i) => {
    const s = sm[String(i)]
    const row = document.createElement('div')
    row.className = `key-row ${i === ai ? 'active' : ''}`
    const dot = document.createElement('span')
    dot.className = `key-dot key-${s ? s.freshness : 'unused'}`
    row.appendChild(dot)
    const val = document.createElement('span')
    val.className = 'key-val'
    val.textContent = maskKey(k)
    val.title = k
    val.onclick = () => copyToClipboard(k)
    row.appendChild(val)
    const acts = document.createElement('div')
    acts.className = 'key-acts'
    if (i === ai) { const b = document.createElement('span'); b.className = 'key-badge'; b.textContent = 'active'; acts.appendChild(b) }
    if (s && s.requests > 0) {
      const info = document.createElement('span')
      info.className = 'key-info'
      info.textContent = `${s.success}✓ ${s.failed > 0 ? s.failed + '✗ ' : ''}${timeAgo(s.last_used)}`
      info.title = s.last_error || ''
      acts.appendChild(info)
    }
    const copyB = document.createElement('button'); copyB.className = 'key-act'; copyB.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; copyB.title = 'Copy'; copyB.onclick = () => copyToClipboard(k)
    acts.appendChild(copyB)
    const delB = document.createElement('button'); delB.className = 'key-act danger'; delB.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'; delB.title = 'Remove'; delB.onclick = () => { keys.splice(i, 1); persistKeys(); toast('Key removed', 'warn') }
    acts.appendChild(delB)
    row.appendChild(acts)
    list.appendChild(row)
  })
}

async function pollKeyStats() {
  try { renderKeys(await api('GET', '/api/keys/stats')) } catch (_) {}
}

async function persistKeys() {
  renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
  try { await api('POST', '/api/keys', { keys }) } catch (e) { toast(`Save failed: ${e.message}`, 'error') }
}

$('addKeyBtn').addEventListener('click', () => { $('keyAddForm').classList.remove('hidden'); $('newKeyInput').value = ''; $('newKeyInput').focus() })
$('cancelAddKeyBtn').addEventListener('click', () => $('keyAddForm').classList.add('hidden'))
$('confirmAddKeyBtn').addEventListener('click', async () => {
  const v = $('newKeyInput').value.trim()
  if (!v) { toast('Enter a key', 'warn'); return }
  if (keys.includes(v)) { toast('Key already exists', 'warn'); return }
  keys.push(v); $('keyAddForm').classList.add('hidden'); await persistKeys(); toast('Key added', 'ok')
})
$('newKeyInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('confirmAddKeyBtn').click() })

/* ── Log ────────────────────────────────────── */
$('clearLogBtn').addEventListener('click', () => {
  $('logArea').innerHTML = '<div class="log-empty">Cleared</div>'; _logEntries = 0; $('logCount').textContent = '0'
})

function appendLog(level, msg) {
  const t = new Date().toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const el = document.createElement('div')
  el.className = `log-entry ${level}`
  el.textContent = `${t}  ${msg}`
  const e = $('logArea').querySelector('.log-empty')
  if (e) e.remove()
  $('logArea').appendChild(el)
  $('logArea').scrollTop = $('logArea').scrollHeight
  _logEntries++
  $('logCount').textContent = _logEntries
  $('logDot').className = 'log-dot active'
  clearTimeout($('logDot')._timer)
  $('logDot')._timer = setTimeout(() => { $('logDot').className = 'log-dot' }, 2000)
}

new EventSource('/api/logs/stream').onmessage = e => {
  try {
    const { msg } = JSON.parse(e.data)
    let level = 'info'
    if (/error|Error|✗/.test(msg)) level = 'error'
    else if (/OK|✔|✓/.test(msg)) level = 'ok'
    appendLog(level, msg)
  } catch (_) {}
}

/* ── Init ───────────────────────────────────── */
;(async () => {
  renderKeys()
  $('usageCode').innerHTML = `<code>${EXAMPLES.curl}</code>`
  try {
    const data = await api('GET', '/api/keys')
    keys = data.keys; renderKeys()
    if (keys.length) toast(`Loaded ${keys.length} keys`, 'ok')
  } catch (_) {}
  try {
    const st = await api('GET', '/api/status')
    toast(`Proxy :${st.port}`, 'ok')
  } catch (_) {}
  await loadModel()
  await loadPresets()
  await fetchModels()
  keyStatsInterval = setInterval(pollKeyStats, 2000)
})()
