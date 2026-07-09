let theme = localStorage.getItem('openvidia-theme') || 'dark'
let keys = []
let accounts = []
let activeAccount = ''
let activeModel = ''
let allModels = []
let presets = []
let _logEntries = 0
let statsInterval, keyStatsInterval
let modelFilter = 'popular'

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

/* ── Running State ──────────────────────────── */
async function updateRunningState() {
  try {
    const st = await api('GET', '/api/status')
    const running = st.running
    $('statusText').textContent = running ? 'Running' : 'Stopped'
    $('statusDot').querySelector('.pulse-dot').className = `pulse-dot ${running ? 'running' : ''}`
    $('stopBtn').style.display = running ? '' : 'none'
    $('startBtn').style.display = running ? 'none' : ''
    $('stopBtn').disabled = false
    $('startBtn').disabled = false
  } catch (_) {
    $('statusText').textContent = 'Offline'
    $('statusDot').querySelector('.pulse-dot').className = 'pulse-dot'
  }
}

$('stopBtn').addEventListener('click', async () => {
  $('stopBtn').disabled = true
  try {
    await api('POST', '/api/stop')
    toast('Proxy stopped', 'warn')
    await updateRunningState()
  } catch (_) { toast('Stop failed', 'error') }
  $('stopBtn').disabled = false
})

$('startBtn').addEventListener('click', async () => {
  $('startBtn').disabled = true
  try {
    await api('POST', '/api/start')
    toast('Proxy started', 'ok')
    await updateRunningState()
  } catch (_) { toast('Start failed', 'error') }
  $('startBtn').disabled = false
})

/* ── Stats ──────────────────────────────────── */
$('portDisplay').textContent = '1919'
statsInterval = setInterval(async () => {
  try {
    const s = await api('GET', '/api/stats')
    $('statReqs').textContent = s.requests
    $('statRots').textContent = s.rotations
    $('statOk').textContent = s.success
    const cd = s.cooldowns || 0
    $('statCooldowns').textContent = cd
    const chip = $('statsChip')
    if (cd > 0) {
      chip.classList.add('warn')
      $('statCooldowns').className = 'stat-num stat-warn stat-blink'
    } else {
      chip.classList.remove('warn')
      $('statCooldowns').className = 'stat-num'
    }
    await updateRunningState()
  } catch (_) {}
}, 2000)

/* ── Model Presets (Home) ───────────────────── */
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
  const grid = $('quickSwitch')
  grid.innerHTML = ''
  $('presetCount').textContent = presets.length
  presets.map(id => ({ id, label: labelForModel(id) })).forEach(p => {
    const btn = document.createElement('button')
    btn.textContent = p.label
    btn.className = p.id === activeModel ? 'active' : ''
    btn.onclick = () => setModel(p.id)
    grid.appendChild(btn)
    if (p.id) {
      const rm = document.createElement('span')
      rm.className = 'preset-rm'
      rm.textContent = '×'
      rm.title = 'Remove preset'
      rm.onclick = e => { e.stopPropagation(); presets = presets.filter(x => x !== p.id); savePresets() }
      btn.appendChild(rm)
    }
  })
  if (!presets.length) {
    grid.innerHTML = '<div style="font-size:.65rem;color:var(--text3);padding:4px 0">Add models from Settings → Available Models</div>'
  }
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
      $('activeModelDisplay').textContent = id || 'none'
      renderPresets()
      renderModelList()
      renderUsage()
      toast(`Model: ${id || 'none'}`, 'ok')
    }
  } catch (_) {}
}

async function loadModel() {
  try {
    const r = await api('GET', '/api/model')
    activeModel = r.model || ''
    $('activeModelDisplay').textContent = activeModel || 'none'
    renderPresets()
  } catch (_) {}
}

/* ── Model Browser (Settings) ────────────────── */
const FILTERS = ['popular', 'all']

function getFilteredModels() {
  let list = allModels
  if (modelFilter === 'popular') {
    list = allModels.filter(m => POPULAR_MODELS.has(m.id))
  }
  const q = ($('modelSearch').value || '').toLowerCase()
  if (q) list = list.filter(m => m.id.toLowerCase().includes(q))
  return list
}

function renderFilters() {
  const cont = $('modelFilters')
  cont.innerHTML = ''
  FILTERS.forEach(f => {
    const btn = document.createElement('button')
    btn.className = `browser-filter ${f === modelFilter ? 'active' : ''}`
    btn.textContent = f === 'popular' ? '★ Popular' : 'All'
    btn.onclick = () => { modelFilter = f; renderFilters(); renderModelList() }
    cont.appendChild(btn)
  })
}

async function fetchModels() {
  try {
    const r = await fetch('/v1/models')
    const d = await r.json()
    allModels = (d.data || []).map(m => ({ id: m.id, owned_by: m.owned_by || '' }))
    renderFilters()
    renderModelList()
  } catch (_) {
    $('modelList').innerHTML = '<div class="browser-empty">Failed to load</div>'
  }
}

async function testModel(id, btn) {
  btn.className = 'browser-test testing'
  btn.textContent = '…'
  const resultEl = document.createElement('div')
  resultEl.className = 'test-result'
  btn.parentElement.parentElement.after(resultEl)
  try {
    const r = await api('POST', '/api/test-model', { model: id })
    if (r.ok) {
      resultEl.className = 'test-result ok'
      resultEl.textContent = `✓ ${(r.response || '').slice(0, 80)}`
      btn.className = 'browser-test done'
      btn.textContent = '✓'
    } else {
      const detail = r.detail || r.error || 'unknown'
      resultEl.className = 'test-result fail'
      resultEl.textContent = `✗ ${detail.slice(0, 120)}`
      btn.className = 'browser-test fail'
      btn.textContent = '✗'
    }
  } catch (e) {
    resultEl.className = 'test-result fail'
    resultEl.textContent = `✗ ${e.message.slice(0, 80)}`
    btn.className = 'browser-test fail'
    btn.textContent = '✗'
  }
  setTimeout(() => resultEl.remove(), 8000)
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
    const inPreset = presets.includes(m.id)
    item.innerHTML = `<span class="browser-name">${m.id}</span>${m.owned_by ? `<span class="browser-owner">${m.owned_by}</span>` : ''}`
    const acts = document.createElement('div')
    acts.className = 'browser-acts'

    const testBtn = document.createElement('button')
    testBtn.className = 'browser-test'
    testBtn.textContent = '▶'
    testBtn.title = 'Test model'
    testBtn.onclick = e => { e.stopPropagation(); testModel(m.id, testBtn) }
    acts.appendChild(testBtn)

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
function renderUsage() {
  const m = activeModel || 'openvidia'
  const examples = {
    curl: `curl http://localhost:1919/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${m}","messages":[{"role":"user","content":"Hello!"}]}'`,
    python: `from openai import OpenAI
client = OpenAI(base_url="http://localhost:1919/v1", api_key="ignored")
r = client.chat.completions.create(
    model="${m}",
    messages=[{"role":"user","content":"Hello!"}]
)
print(r.choices[0].message.content)`,
    js: `const r = await fetch("http://localhost:1919/v1/chat/completions", {
  method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({model:"${m}",
    messages:[{role:"user",content:"Hello!"}]})
})
const d = await r.json()
console.log(d.choices[0].message.content)`,
  }
  $('usageCode').innerHTML = `<code>${examples[currentLang]}</code>`
}
let currentLang = 'curl'
document.querySelectorAll('.usage-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.usage-tab').forEach(t => t.classList.remove('active'))
    tab.classList.add('active')
    currentLang = tab.dataset.usage
    renderUsage()
  })
})

/* ── Keys / Accounts ─────────────────────────── */
function maskKey(k) { return k.length <= 14 ? k : `${k.slice(0, 8)}…${k.slice(-4)}` }
function copyToClipboard(t) { navigator.clipboard.writeText(t).then(() => toast('Key copied', 'ok')).catch(() => {}) }
function timeAgo(ts) {
  if (!ts) return ''
  const sec = Math.floor(Date.now() / 1000 - ts)
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec / 3600)}h`
}

function getCurrentAccountName() {
  const sel = $('accountSelect')
  return sel ? sel.value : activeAccount
}

function renderAccountSelect() {
  const sel = $('accountSelect')
  if (!sel) return
  sel.innerHTML = ''
  const allOpt = document.createElement('option')
  allOpt.value = ''; allOpt.textContent = 'All accounts'
  sel.appendChild(allOpt)
  accounts.forEach(a => {
    const opt = document.createElement('option')
    opt.value = a.name
    opt.textContent = `${a.name} (${(a.keys || []).length})`
    sel.appendChild(opt)
  })
  sel.value = activeAccount

  // Also populate keyAccountSelect
  const kaSel = $('keyAccountSelect')
  if (kaSel) {
    kaSel.innerHTML = ''
    accounts.forEach(a => {
      const opt = document.createElement('option')
      opt.value = a.name
      opt.textContent = a.name
      kaSel.appendChild(opt)
    })
    kaSel.value = activeAccount || (accounts.length ? accounts[0].name : 'default')
  }

  // Render account manager in the account bar
  const mgr = $('accountManager')
  if (!mgr) return
  mgr.innerHTML = ''
  accounts.forEach(a => {
    const row = document.createElement('div')
    row.className = 'acct-row'
    const label = document.createElement('span')
    label.className = 'acct-name'
    label.textContent = `${a.name} (${(a.keys || []).length})`
    row.appendChild(label)
    const delBtn = document.createElement('button')
    delBtn.className = 'key-act danger'
    delBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    delBtn.title = 'Delete account'
    delBtn.onclick = () => deleteAccount(a.name)
    row.appendChild(delBtn)
    mgr.appendChild(row)
  })
}

async function deleteAccount(name) {
  if (!confirm(`Delete account "${name}" and all its keys?`)) return
  accounts = accounts.filter(a => a.name !== name)
  if (activeAccount === name) activeAccount = ''
  await persistKeys()
  toast(`Account "${name}" deleted`, 'warn')
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
    const isOnCooldown = s && s.cooldown > 0
    const row = document.createElement('div')
    row.className = `key-row ${i === ai ? 'active' : ''}`
    const dot = document.createElement('span')
    let dotClass = 'stale'
    if (isOnCooldown) dotClass = 'dead'
    else if (s && s.success > 0) dotClass = 'fresh'
    else if (s && s.requests > 0) dotClass = 'stale'
    dot.className = `key-dot key-${dotClass}`
    row.appendChild(dot)
    const val = document.createElement('span')
    val.className = 'key-val'
    val.textContent = maskKey(k)
    val.title = k
    val.onclick = () => copyToClipboard(k)
    row.appendChild(val)
    if (s && s.account) {
      const acct = document.createElement('span')
      acct.className = 'key-account-label'
      acct.textContent = s.account
      row.appendChild(acct)
    }
    const acts = document.createElement('div')
    acts.className = 'key-acts'
    const info = document.createElement('span')
    info.className = 'key-info'
    if (isOnCooldown) {
      const sec = Math.ceil(s.cooldown)
      info.textContent = `⏳ ${sec}s  ${s.cooldown_reason}`
      info.title = s.cooldown_reason
    } else if (s && s.requests > 0) {
      info.textContent = `${s.success}✓ ${s.failed > 0 ? s.failed + '✗ ' : ''}${timeAgo(s.last_used)} · ${s.rpm} RPM`
      info.title = s.last_error || ''
    } else {
      info.textContent = `idle · ${s && s.rpm || 0} RPM`
      info.title = ''
    }
    if (i === ai) {
      const b = document.createElement('span')
      b.className = 'key-badge'
      b.textContent = isOnCooldown ? '⏳' : 'active'
      acts.appendChild(b)
    }
    acts.appendChild(info)
    const copyB = document.createElement('button'); copyB.className = 'key-act'; copyB.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'; copyB.title = 'Copy'; copyB.onclick = () => copyToClipboard(k)
    acts.appendChild(copyB)
    const delB = document.createElement('button'); delB.className = 'key-act danger'; delB.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'; delB.title = 'Remove'; delB.onclick = () => {
      const acctName = s ? s.account : 'default'
      const acct = accounts.find(a => a.name === acctName)
      if (acct) {
        acct.keys = acct.keys.filter(x => x !== k)
        keys = keys.filter(x => x !== k)
      }
      persistKeys()
      toast('Key removed', 'warn')
    }
    acts.appendChild(delB)
    row.appendChild(acts)
    list.appendChild(row)
  })
}

async function pollKeyStats() {
  try { renderKeys(await api('GET', '/api/keys/stats')) } catch (_) {}
}

async function persistKeys() {
  // Rebuild accounts from current state
  const flat = []
  accounts.forEach(a => { flat.push(...a.keys) })
  keys = flat
  renderAccountSelect()
  renderKeys(await api('GET', '/api/keys/stats').catch(() => null))
  try {
    await api('POST', '/api/accounts', { accounts })
  } catch (e) { toast(`Save failed: ${e.message}`, 'error') }
}

async function switchAccount(name) {
  activeAccount = name
  try {
    await api('POST', '/api/accounts/active', { account: name })
    toast(`Account: ${name || 'All'}`, 'ok')
  } catch (_) {}
}

$('accountSelect') && $('accountSelect').addEventListener('change', async e => {
  await switchAccount(e.target.value)
  // Re-fetch keys after switching
  try {
    const d = await api('GET', '/api/keys')
    keys = d.keys
    renderKeys()
  } catch (_) {}
})

$('addAccountBtn') && $('addAccountBtn').addEventListener('click', async () => {
  const name = prompt('Account name:')
  if (!name || !name.trim()) return
  if (accounts.find(a => a.name === name.trim())) {
    toast('Account already exists', 'warn')
    return
  }
  accounts.push({ name: name.trim(), keys: [] })
  await persistKeys()
  await switchAccount(name.trim())
  toast(`Account "${name.trim()}" created`, 'ok')
})

$('addKeyBtn').addEventListener('click', () => {
  $('keyAddForm').classList.remove('hidden')
  $('newKeyInput').value = ''
  // Populate keyAccountSelect
  const kaSel = $('keyAccountSelect')
  if (kaSel) {
    kaSel.innerHTML = ''
    accounts.forEach(a => {
      const opt = document.createElement('option')
      opt.value = a.name
      opt.textContent = a.name
      kaSel.appendChild(opt)
    })
    kaSel.value = activeAccount || (accounts.length ? accounts[0].name : 'default')
  }
  $('newKeyInput').focus()
})
$('cancelAddKeyBtn').addEventListener('click', () => $('keyAddForm').classList.add('hidden'))
$('confirmAddKeyBtn').addEventListener('click', async () => {
  const v = $('newKeyInput').value.trim()
  if (!v) { toast('Enter a key', 'warn'); return }
  if (keys.includes(v)) { toast('Key already exists', 'warn'); return }
  // Find target account
  const kaSel = $('keyAccountSelect')
  const acctName = kaSel ? kaSel.value : (accounts.length ? accounts[0].name : 'default')
  let acct = accounts.find(a => a.name === acctName)
  if (!acct) { acct = { name: acctName, keys: [] }; accounts.push(acct) }
  acct.keys.push(v)
  keys.push(v)
  $('keyAddForm').classList.add('hidden')
  await persistKeys()
  toast('Key added', 'ok')
})
$('newKeyInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('confirmAddKeyBtn').click() })

/* ── Log ────────────────────────────────────── */
$('clearLogBtn').addEventListener('click', () => {
  $('logArea').innerHTML = '<div class="log-empty">Cleared</div>'; _logEntries = 0; $('logCount').textContent = '0'
})

$('restartBtn').addEventListener('click', async () => {
  $('restartBtn').disabled = true
  $('restartBtn').textContent = '⟳ …'
  try {
    await api('POST', '/api/restart')
    toast('Restarting…', 'info')
  } catch (_) {
    toast('Restart failed', 'error')
    $('restartBtn').disabled = false
    $('restartBtn').textContent = '⟳ Restart'
  }
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

/* ── News ───────────────────────────────────── */
async function loadNews() {
  try {
    const d = await api('GET', '/api/news')
    const news = d.news || []
    const list = $('newsList')
    $('newsCount').textContent = news.length
    if (!news.length) {
      list.innerHTML = '<div class="empty-state"><div class="empty-ttl">No updates</div><div class="empty-desc">Check back later</div></div>'
      return
    }
    list.innerHTML = ''
    news.forEach(n => {
      const item = document.createElement('div')
      item.className = 'news-item'
      const title = document.createElement('a')
      title.href = n.url
      title.target = '_blank'
      title.textContent = n.title
      item.appendChild(title)
      if (n.excerpt) {
        const exc = document.createElement('div')
        exc.className = 'news-excerpt'
        exc.textContent = n.excerpt.replace(/<[^>]+>/g, '').slice(0, 200)
        item.appendChild(exc)
      }
      const meta = document.createElement('div')
      meta.className = 'news-meta'
      meta.textContent = [n.author, n.time.slice(0, 10)].filter(Boolean).join(' · ')
      item.appendChild(meta)
      list.appendChild(item)
    })
  } catch (_) {
    $('newsList').innerHTML = '<div class="empty-state"><div class="empty-ttl">Failed to load</div></div>'
  }
}

/* ── Init ───────────────────────────────────── */
;(async () => {
  renderKeys()
  renderUsage()
  try {
    const d = await api('GET', '/api/accounts')
    accounts = d.accounts || []
    activeAccount = d.active_account || ''
    keys = []
    accounts.forEach(a => { keys.push(...a.keys) })
    renderAccountSelect()
    renderKeys()
    if (keys.length) toast(`Loaded ${keys.length} keys across ${accounts.length} account(s)`, 'ok')
  } catch (_) {}
  await updateRunningState()
  await loadModel()
  await loadPresets()
  await fetchModels()
  await loadNews()
  keyStatsInterval = setInterval(pollKeyStats, 2000)
})()
