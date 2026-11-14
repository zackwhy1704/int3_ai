// int3 launcher UI. Every action is a Rust command (src-tauri/src/main.rs); this page
// never sees a token and stores nothing.
'use strict'

const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args)
const $ = id => document.getElementById(id)

function show(view) {
  for (const v of ['signin', 'app']) $(`view-${v}`).hidden = v !== view
}

function tab(name) {
  for (const b of document.querySelectorAll('.nav')) b.classList.toggle('active', b.dataset.tab === name)
  for (const t of ['library', 'memory', 'account']) $(`tab-${t}`).hidden = t !== name
  if (name === 'account') loadAccount()
  if (name === 'memory') loadMemory()
}

function message(e) {
  return (e && e.message) || String(e)
}

// Any command can report that this device was signed out (revoked elsewhere, refresh expired).
async function call(cmd, args) {
  try {
    return await invoke(cmd, args)
  } catch (e) {
    if (e && e.kind === 'signed_out') {
      show('signin')
      $('signin-error').textContent = 'You were signed out on this device. Sign in again with a new invite.'
      $('signin-error').hidden = false
    }
    throw e
  }
}

function text(el, value) {
  el.textContent = value == null ? '' : String(value)
  return el
}

function row(dl, term, value) {
  dl.append(text(document.createElement('dt'), term), text(document.createElement('dd'), value))
}

let launchMode = 'hosted'

function renderMe(me) {
  launchMode = (me.tenant && me.tenant.launch_mode) || 'hosted'
  $('choose-model').hidden = launchMode !== 'local'
  $('nav-memory').hidden = launchMode !== 'local'
  text($('who-name'), me.user.name)
  text($('who-tenant'), me.tenant.name)
  text($('hero-tenant'), me.tenant.name)
  const list = $('brains')
  list.replaceChildren()
  for (const b of me.brains) {
    const li = document.createElement('li')
    li.className = 'brain'
    const tile = document.createElement('span')
    tile.className = 'tile'
    tile.textContent = (b.name || '?').slice(0, 1).toUpperCase()
    li.append(tile, text(document.createElement('span'), b.name))
    list.append(li)
  }
}

async function refreshHermesLine() {
  const line = $('hermes-line')
  try {
    const h = await call('hermes_status')
    $('get-hermes').hidden = h.found
    if (!h.found) {
      line.textContent = h.os_supported
        ? 'Hermes Desktop is not installed.'
        : `Hermes Desktop needs Windows 11 (version 22H2 or later). This PC runs Windows build ${h.os_build}.`
      $('launch').disabled = true
      return
    }
    $('launch').disabled = h.policy.state === 'blocked'
    // The exact version stays on screen so it can be screenshotted and recorded (store-test.sh --record).
    line.textContent = `Hermes version: ${h.version === 'dev' ? 'development build' : h.version}`
    const note = $('hermes-note')
    note.textContent = h.policy.state === 'supported' ? '' : h.policy.message
    note.hidden = h.policy.state === 'supported'
  } catch (e) {
    line.textContent = message(e)
  }
}

async function enterApp() {
  const me = await call('me')
  renderMe(me)
  show('app')
  tab('library')
  refreshHermesLine()
}

async function launch(restart) {
  const msg = $('launch-msg')
  msg.hidden = false
  msg.className = 'notice'
  msg.textContent = 'Starting Hermes…'
  $('launch').disabled = true
  try {
    const r = await call(launchMode === 'local' ? 'launch_local' : 'launch', { restart })
    if (r.needs_restart) {
      msg.hidden = true
      $('restart-dialog').showModal()
      return
    }
    const until = new Date(r.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (r.mode === 'local') {
      msg.textContent = `Hermes is starting on this PC, connected to your company brain until ${until}. The assistant can save files only in ${r.workspace}. To pick or change the AI model, use "Choose AI model".`
      return
    }
    msg.textContent = `Hermes is starting, connected as ${r.label}. ${r.backend_online ? 'Your backend is online.' : 'Your backend did not answer yet.'} This connection is valid until ${until}; launch again from here after that.`
  } catch (e) {
    msg.className = 'notice error'
    msg.textContent = message(e)
  } finally {
    $('launch').disabled = false
  }
}

async function loadAccount() {
  const meDl = $('acct-me')
  const health = $('acct-health')
  const devices = $('devices')
  meDl.replaceChildren(); health.replaceChildren(); devices.replaceChildren()
  try {
    const a = await call('account')
    row(meDl, 'Name', a.me.user.name)
    row(meDl, 'Email', a.me.user.email)
    row(meDl, 'Company', a.me.tenant.name)
    row(meDl, 'Role', a.me.user.is_admin ? 'Admin' : 'Member')
    row(health, 'Server', a.server)
    row(health, 'Server status', 'Reachable')
    row(health, 'Backend Hermes', a.backend_release)
    $('acct-byok').hidden = !a.me.user.is_admin
    for (const d of a.devices) {
      const li = document.createElement('li')
      const label = text(document.createElement('span'), `${d.label} · ${d.platform}${d.current ? ' · this device' : ''}`)
      const state = text(document.createElement('span'), d.revoked ? 'signed out' : `last seen ${new Date(d.last_seen || d.created_at).toLocaleString()}`)
      state.className = 'muted small'
      li.append(label, state)
      if (!d.revoked && !d.current) {
        const b = text(document.createElement('button'), 'Sign out device')
        b.className = 'secondary small'
        b.onclick = async () => { await call('revoke_device', { deviceId: d.id }); loadAccount() }
        li.append(b)
      }
      devices.append(li)
    }
  } catch (e) {
    row(health, 'Server status', message(e))
  }
}

// Local mode M2: the user's memories as kept by int3. Content is shown as text only (textContent).
async function loadMemory() {
  const list = $('memories')
  const msg = $('memory-msg')
  list.replaceChildren()
  msg.hidden = true
  try {
    const r = await call('memory_list')
    $('memory-clear').hidden = r.total === 0
    if (r.total === 0) {
      msg.hidden = false
      msg.className = 'notice'
      msg.textContent = 'Nothing saved yet. Ask the assistant to "remember" something and it will show up here.'
      return
    }
    for (const m of r.memories) {
      const li = document.createElement('li')
      const body = text(document.createElement('span'), (m.kind === 'preference' ? 'Preference: ' : '') + m.content)
      const when = text(document.createElement('span'), new Date(m.created_at).toLocaleString())
      when.className = 'muted small'
      const b = text(document.createElement('button'), 'Delete')
      b.className = 'secondary small'
      b.onclick = async () => {
        b.disabled = true
        try { await call('memory_delete', { id: m.id, all: false }) } catch (e) { b.disabled = false; return memoryError(e) }
        loadMemory()
      }
      li.append(body, when, b)
      list.append(li)
    }
    if (r.total > r.memories.length) {
      msg.hidden = false
      msg.className = 'notice'
      msg.textContent = `Showing the newest ${r.memories.length} of ${r.total}.`
    }
  } catch (e) {
    memoryError(e)
  }
}

function memoryError(e) {
  const msg = $('memory-msg')
  msg.hidden = false
  msg.className = 'notice error'
  msg.textContent = message(e)
}

async function signOut() {
  await call('sign_out').catch(() => {})
  $('invite').value = ''
  $('signin-error').hidden = true
  $('launch-msg').hidden = true // the previous user's launch notice must not carry over
  show('signin')
}

window.addEventListener('DOMContentLoaded', async () => {
  for (const b of document.querySelectorAll('.nav')) b.onclick = () => tab(b.dataset.tab)
  const signIn = async () => {
    const err = $('signin-error')
    err.hidden = true
    const inviteLink = $('invite').value
    $('invite').value = '' // single-use credential: never left in the field
    try {
      await call('sign_in', { inviteLink })
      await enterApp()
    } catch (e) {
      err.textContent = message(e)
      err.hidden = false
    }
  }
  $('signin').onclick = signIn
  $('invite').onkeydown = ev => { if (ev.key === 'Enter') signIn() }
  $('launch').onclick = () => launch(false)
  $('get-hermes').onclick = () => call('open_hermes_download').then(refreshHermesLine, () => {})
  $('choose-model').onclick = () => call('open_providers').catch(e => { $('launch-msg').hidden = false; $('launch-msg').className = 'notice error'; $('launch-msg').textContent = message(e) })
  $('restart-ok').onclick = () => { $('restart-dialog').close(); launch(true) }
  $('restart-cancel').onclick = () => $('restart-dialog').close()
  $('memory-refresh').onclick = loadMemory
  $('memory-clear').onclick = () => $('clear-dialog').showModal()
  $('clear-cancel').onclick = () => $('clear-dialog').close()
  $('clear-ok').onclick = async () => {
    $('clear-dialog').close()
    try { await call('memory_delete', { id: null, all: true }) } catch (e) { return memoryError(e) }
    loadMemory()
  }
  $('sign-out').onclick = signOut
  $('switch-user').onclick = signOut
  $('byok').onclick = async () => {
    try {
      const r = await call('byok_link')
      $('byok-msg').textContent = `Opened in your browser. The link works once, for ${r.expires_in_hours} hours.`
    } catch (e) {
      $('byok-msg').textContent = message(e)
    }
  }

  const s = await invoke('status')
  if (!s.signed_in) return show('signin')
  try {
    await enterApp()
  } catch (e) {
    if (!(e && e.kind === 'signed_out')) {
      show('signin')
      $('signin-error').textContent = message(e)
      $('signin-error').hidden = false
    }
  }
})
