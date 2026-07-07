/* ═══════════════════════════════════════════════
   OpenVidia — Frontend Controller
   SOTA 2026 · Glassmorphism · Login Auth · Log in Home
   ═══════════════════════════════════════════════ */

let statsInterval = null
let keys = []
let accounts = []

/* ── DOM refs ────────────────────────────────── */
const $ = (id) => document.getElementById(id)

const statusDot = $('statusDot')
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

const accountsList = $('accountsList')
const accountsEmpty = $('accountsEmpty')
const accountCount = $('accountCount')
const addAccountBtn = $('addAccountBtn')
const accountAddForm = $('accountAddForm')
const accountNameInput = $('accountNameInput')
const accountEmailInput = $('accountEmailInput')
const accountPasswordInput = $('accountPasswordInput')
const accountCookiesInput = $('accountCookiesInput')
const confirmAddAccountBtn = $('confirmAddAccountBtn')
const cancelAddAccountBtn = $('cancelAddAccountBtn')

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
  el.innerHTML = `
    <div class="toast-icon ${level}">${icons[level] || icons.info}</div>
    <div class="toast-msg">${msg}</div>`
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

/* ── Stats ──────────────────────────────────── */
statusDot.innerHTML = '<span class="pulse-dot running"></span>'
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

/* ── Key helpers ────────────────────────────── */
function maskKey(k) {
  return k.length <= 14 ? k : `${k.slice(0, 8)}…${k.slice(-4)}`
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => toast('Key copied to clipboard', 'ok')).catch(() => {})
}

/* ── Render keys ────────────────────────────── */
function renderKeys() {
  keysList.innerHTML = ''
  if (!keys.length) {
    keysEmpty.style.display = 'flex'
    keyCount.textContent = '0'
    return
  }
  keysEmpty.style.display = 'none'
  keyCount.textContent = String(keys.length)
  keys.forEach((k, i) => {
    const row = document.createElement('div'); row.className = 'key-row'
    const val = document.createElement('span'); val.className = 'key-value'
    val.textContent = maskKey(k); val.title = k
    val.addEventListener('click', () => copyToClipboard(k))
    const group = document.createElement('div'); group.className = 'key-actions-group'
    const copyBtn = document.createElement('button'); copyBtn.className = 'key-action-btn'
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    copyBtn.title = 'Copy key'; copyBtn.addEventListener('click', () => copyToClipboard(k))
    group.appendChild(copyBtn)
    const delBtn = document.createElement('button'); delBtn.className = 'key-action-btn danger'
    delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    delBtn.title = 'Remove key'; delBtn.addEventListener('click', () => { keys.splice(i, 1); persistKeys(); toast('Key removed', 'warn') })
    group.appendChild(delBtn)
    row.appendChild(val); row.appendChild(group)
    keysList.appendChild(row)
  })
}

async function persistKeys() {
  renderKeys()
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

/* ── Accounts ───────────────────────────────── */
function renderAccounts() {
  if (!accountsList) return
  accountsList.innerHTML = ''
  if (!accounts.length) {
    accountsEmpty.style.display = 'flex'
    accountCount.textContent = '0'
    return
  }
  accountsEmpty.style.display = 'none'
  accountCount.textContent = String(accounts.length)
  accounts.forEach(a => {
    const card = document.createElement('div'); card.className = 'account-card'
    const header = document.createElement('div'); header.className = 'account-header'
    const nameEl = document.createElement('div'); nameEl.className = 'account-name'
    const icon = document.createElement('div'); icon.className = 'account-name-icon'
    icon.textContent = a.name.charAt(0).toUpperCase()
    const txt = document.createElement('span'); txt.textContent = a.name
    nameEl.appendChild(icon); nameEl.appendChild(txt)
    const badge = document.createElement('div'); badge.className = 'account-key-badge'
    badge.textContent = `${a.key_count} key${a.key_count !== 1 ? 's' : ''}`
    header.appendChild(nameEl); header.appendChild(badge)
    card.appendChild(header)

    // Auth type indicator
    const authRow = document.createElement('div'); authRow.className = 'account-auth-row'
    if (a.has_credentials) {
      authRow.innerHTML = `<span class="auth-badge auth-email">🔑 ${a.email}</span>`
    } else if (a.cookies_preview) {
      authRow.innerHTML = `<span class="auth-badge auth-cookie">🍪 cookies</span>`
    } else {
      authRow.innerHTML = `<span class="auth-badge auth-none">⚠ no auth</span>`
    }
    card.appendChild(authRow)

    const actions = document.createElement('div'); actions.className = 'account-actions'
    const renewBtn = document.createElement('button'); renewBtn.className = 'btn btn-sm'
    renewBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Replenish`
    renewBtn.title = 'Generate a fresh key to replace the oldest one'
    renewBtn.addEventListener('click', async () => {
      renewBtn.disabled = true; renewBtn.textContent = '…'
      try {
        await api('POST', `/api/accounts/${encodeURIComponent(a.name)}/replenish`)
        toast(`Replenish triggered for ${a.name}`, 'ok')
      } catch (e) { toast(`Replenish failed: ${e.message}`, 'error') }
      setTimeout(() => { renewBtn.disabled = false; renewBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Replenish` }, 2000)
    })
    actions.appendChild(renewBtn)
    const delBtn = document.createElement('button'); delBtn.className = 'btn btn-sm btn-danger'
    delBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg> Remove`
    delBtn.addEventListener('click', async () => {
      if (!confirm(`Remove "${a.name}" and all its keys?`)) return
      try {
        await api('DELETE', `/api/accounts/${encodeURIComponent(a.name)}`)
        toast(`Account ${a.name} removed`, 'ok')
        await loadAccounts()
      } catch (e) { toast(`Remove failed: ${e.message}`, 'error') }
    })
    actions.appendChild(delBtn)
    card.appendChild(actions)
    accountsList.appendChild(card)
  })
}

async function loadAccounts() {
  try {
    const data = await api('GET', '/api/accounts')
    accounts = data.accounts
    renderAccounts()
  } catch (_) {}
}

/* ── Account add form ──────────────────────── */
addAccountBtn.addEventListener('click', () => {
  accountAddForm.classList.remove('hidden')
  accountNameInput.value = ''; accountEmailInput.value = ''; accountPasswordInput.value = ''; accountCookiesInput.value = ''
  accountNameInput.focus()
})
cancelAddAccountBtn.addEventListener('click', () => accountAddForm.classList.add('hidden'))
confirmAddAccountBtn.addEventListener('click', async () => {
  const name = accountNameInput.value.trim()
  const email = accountEmailInput.value.trim()
  const password = accountPasswordInput.value
  const cookies = accountCookiesInput.value.trim()
  if (!name) { toast('Account name required', 'warn'); return }
  if (!email && !cookies) { toast('Email+password or cookies required', 'warn'); return }
  if (email && !password) { toast('Password required for email login', 'warn'); return }

  confirmAddAccountBtn.disabled = true; confirmAddAccountBtn.textContent = 'Saving…'
  try {
    const res = await api('POST', '/api/accounts', { name, email, password, cookies })
    if (!res.ok) { toast(res.error || 'Add failed', 'error'); return }
    accountAddForm.classList.add('hidden')
    toast(`Account ${name} added`, 'ok')
    await loadAccounts()
  } catch (e) { toast(`Add failed: ${e.message}`, 'error') }
  finally { confirmAddAccountBtn.disabled = false; confirmAddAccountBtn.textContent = 'Save Account' }
})

/* ── Log (now on Home tab) ──────────────────── */
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
  try {
    const data = await api('GET', '/api/keys')
    keys = data.keys; renderKeys()
    if (keys.length) toast(`Loaded ${keys.length} keys`, 'ok')
  } catch (_) {}
  try {
    const st = await api('GET', '/api/status')
    toast(`Proxy running on :${st.port}`, 'ok')
  } catch (_) {}
  await loadAccounts()
})()
