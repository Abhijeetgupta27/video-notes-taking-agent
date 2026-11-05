const STEPS = [
  ['QUEUED', 'Queued'],
  ['TRANSCRIBING', 'Transcribing'],
  ['SUMMARIZING', 'Summarizing'],
  ['EXTRACTING_FRAMES', 'Extracting Frames'],
  ['PUBLISHING', 'Publishing'],
  ['DONE', 'Done']
]
const TERMINAL = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'DEAD_LETTERED']

const PILL = {
  QUEUED: ['Queued', ''],
  TRANSCRIBING: ['Working', 'run'],
  SUMMARIZING: ['Working', 'run'],
  EXTRACTING_FRAMES: ['Working', 'run'],
  PUBLISHING: ['Working', 'run'],
  COMPLETED: ['Done', 'ok'],
  COMPLETED_WITH_WARNINGS: ['Done · warnings', 'warn'],
  FAILED: ['Failed', 'err'],
  DEAD_LETTERED: ['Gave up', 'err']
}

// friendlier text for error codes the backend can send
const ERR_TEXT = {
  VIDEO_TOO_LONG: 'This video is longer than 20 minutes. Only videos up to 20:00 are supported.',
  NO_TRANSCRIPT: "This video has no captions, so there's nothing to take notes from.",
  LIVE_VIDEO: "Live streams can't be processed.",
  NOTION_UNAUTHORIZED: 'Notion access was revoked or expired. Reconnect Notion and try again.',
  NOTION_NO_ACCESS: "The integration can't open the Notion page. Share the page with it (••• → Connections) and retry.",
  NO_NOTION_DESTINATION: "The integration can't see any Notion page. Share a page called \"Video Notes\" with it.",
  LLM_AUTH: 'The backend LLM key was rejected (server config problem).',
  LOST: 'The backend lost track of this job (was Redis wiped?).'
}

const WARN_TEXT = {
  FRAME_EXTRACTION_FAILED: t => `Couldn't capture the frame at ${fmt(t)} - left it out`,
  FRAME_UPLOAD_FAILED: t => `Couldn't upload the frame at ${fmt(t)} - left it out`,
  TRANSCRIPT_NOT_ENGLISH: () => 'Transcript was not in English'
}

const $ = s => document.querySelector(s)

function fmt(sec) {
  if (sec == null) return '?'
  sec = Math.round(sec)
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

function send(msg) {
  return chrome.runtime.sendMessage(msg).catch(e => ({ ok: false, error: e.message }))
}

// ---------------- rendering ----------------

function stepIndex(status) {
  if (TERMINAL.includes(status) && status.startsWith('COMPLETED')) return 5
  return STEPS.findIndex(s => s[0] === status)
}

function renderSteps(ol, job) {
  ol.innerHTML = ''
  const failed = job.status === 'FAILED' || job.status === 'DEAD_LETTERED'
  // for failed jobs, highlight the stage it died in
  const stageMap = { transcribe: 1, summarize: 2, frames: 3, publish: 4 }
  const cur = failed ? (stageMap[job.error?.stage] ?? 1) : stepIndex(job.status)
  STEPS.forEach(([, label], i) => {
    const li = document.createElement('li')
    li.textContent = label
    if (i < cur || (i === 5 && cur === 5)) li.className = 'done'
    else if (i === cur) li.className = failed ? 'bad' : 'now'
    ol.appendChild(li)
  })
}

function detailText(job) {
  if (job.offline) return { text: "Can't reach the backend right now, will keep trying…", cls: 'waiting' }
  const w = job.waiting
  if (w && !TERMINAL.includes(job.status)) {
    const left = Math.max(0, Math.round(w.retry_at - Date.now() / 1000))
    const why = w.code === 'RATE_LIMITED' ? 'Rate limited' : 'Hit a temporary problem'
    return {
      text: `${why} - retrying ${left ? `in ${left}s` : 'now'} (attempt ${w.attempt + 1} of ${w.max_attempts})`,
      cls: 'waiting'
    }
  }
  if (job.status === 'COMPLETED') return { text: framesLine(job) }
  if (job.status === 'COMPLETED_WITH_WARNINGS') return { text: framesLine(job) }
  if (TERMINAL.includes(job.status)) return { text: '' }
  return { text: job.detail || 'Waiting for a worker…' }
}

function framesLine(job) {
  const r = job.result || {}
  const dur = job.duration_sec ? ` · ${fmt(job.duration_sec)} video` : ''
  if (!r.frames_requested) return `Text notes published${dur}`
  return `${r.frames_published} of ${r.frames_requested} frames included${dur}`
}

function renderJob(job) {
  const el = $('#job-tpl').content.firstElementChild.cloneNode(true)
  el.dataset.id = job.job_id
  el.querySelector('.title').textContent = job.video_title || job.video_id
  el.querySelector('.title').title = job.video_title || ''

  const [pillText, pillCls] = PILL[job.status] || [job.status, '']
  const pill = el.querySelector('.pill')
  pill.textContent = pillText
  if (pillCls) pill.classList.add(pillCls)

  renderSteps(el.querySelector('.steps'), job)

  const done = job.status?.startsWith('COMPLETED')
  const failed = job.status === 'FAILED' || job.status === 'DEAD_LETTERED'
  if (done) el.classList.add('finished')
  if (failed) el.classList.add('failed')
  el.querySelector('.fill').style.width = `${done || failed ? 100 : (job.progress || 2)}%`

  const d = detailText(job)
  const det = el.querySelector('.detail')
  det.textContent = d.text
  if (d.cls) det.classList.add(d.cls)
  if (!d.text) det.remove()

  const warns = (job.warnings || [])
  if (warns.length && TERMINAL.includes(job.status)) {
    const box = el.querySelector('.box.warn')
    const items = warns.map(w => `<li>${esc((WARN_TEXT[w.code] || (() => w.message || w.code))(w.timestamp_sec))}</li>`)
    box.innerHTML = `Published with ${warns.length === 1 ? 'a warning' : warns.length + ' warnings'}:<ul>${items.join('')}</ul>`
    box.hidden = false
  }

  if (failed) {
    const box = el.querySelector('.box.error')
    const e = job.error || {}
    let text = ERR_TEXT[e.code] || e.message || 'Something went wrong.'
    if (job.status === 'DEAD_LETTERED') {
      text = e.code === 'WORKER_CRASH_LOOP'
        ? 'This job kept crashing workers, so it was parked in the dead-letter queue.'
        : `Gave up after ${e.attempts || 'several'} attempts. ${e.message?.replace(/^Gave up after \d+ attempts\. /, '') || ''}`
      text += ' It can be inspected in the DLQ.'
    }
    box.textContent = text
    box.hidden = false
    if (e.code === 'NOTION_UNAUTHORIZED') addReconnect(el.querySelector('.actions'))
  }

  const url = job.result?.notion_page_url
  if (done && url) {
    const a = document.createElement('a')
    a.className = 'notion'
    a.href = url
    a.target = '_blank'
    a.textContent = 'View in Notion ↗'
    el.querySelector('.actions').appendChild(a)
  }
  return el
}

function addReconnect(actions) {
  const b = document.createElement('button')
  b.className = 'small'
  b.textContent = 'Reconnect Notion'
  b.onclick = connect
  actions.appendChild(b)
}

function render(jobs) {
  const box = $('#jobs')
  box.innerHTML = ''
  if (!jobs || !jobs.length) {
    box.innerHTML = '<div class="empty">No notes yet.<br>Open a YouTube video and click <b>Take Notes</b>.</div>'
    return
  }
  jobs.forEach(j => box.appendChild(renderJob(j)))
}

async function renderNotion() {
  const { notion } = await chrome.storage.local.get('notion')
  const bar = $('#notion-bar')
  if (notion?.access_token) {
    bar.innerHTML = `<span><span class="dot on"></span>Notion: <b>${esc(notion.workspace_name || 'connected')}</b></span>
      <button class="link" id="disconnect">Disconnect</button>`
    $('#disconnect').onclick = async () => { await send({ type: 'disconnect' }); renderNotion() }
  } else {
    bar.innerHTML = `<span><span class="dot"></span>Notion not connected</span>
      <button class="small primary" id="connect">Connect Notion</button>`
    $('#connect').onclick = connect
  }
}

async function connect() {
  const r = await send({ type: 'connect' })
  if (!r.ok) alertBar(r.error)
  renderNotion()
}

function alertBar(text) {
  const bar = $('#notion-bar')
  bar.innerHTML = `<span style="color:var(--err)">${esc(text)}</span>`
  setTimeout(renderNotion, 4000)
}

// ---------------- settings ----------------

async function loadSettings() {
  const s = await chrome.storage.sync.get(['apiBase', 'parentId'])
  $('#api-base').value = s.apiBase || 'http://localhost:8000'
  $('#parent-id').value = s.parentId || ''
}

$('#gear').onclick = () => { $('#settings').hidden = !$('#settings').hidden }

$('#save-settings').onclick = async () => {
  const apiBase = $('#api-base').value.trim().replace(/\/+$/, '') || 'http://localhost:8000'
  // other hosts than localhost need an extra permission
  if (!/^http:\/\/(localhost|127\.0\.0\.1):8000$/.test(apiBase)) {
    const origin = new URL(apiBase).origin + '/*'
    const okPerm = await chrome.permissions.request({ origins: [origin] })
    if (!okPerm) { $('#health').textContent = 'permission denied'; return }
  }
  await chrome.storage.sync.set({ apiBase, parentId: $('#parent-id').value.trim() })
  $('#health').textContent = 'saved'
}

$('#save-token').onclick = async () => {
  const t = $('#manual-token').value.trim()
  if (t.length < 10) return
  await send({ type: 'setToken', token: t })
  $('#manual-token').value = ''
  renderNotion()
}

$('#check-health').onclick = async () => {
  $('#health').textContent = 'checking…'
  const r = await send({ type: 'health' })
  if (r.ok) {
    const d = r.data
    $('#health').textContent = `ok · ${d.workers_alive} worker(s) · ${d.queue_depth} queued`
  } else {
    $('#health').textContent = r.error || `backend: ${r.data?.status || 'down'}`
  }
}

$('#clear').onclick = async () => {
  const r = await send({ type: 'clearDone' })
  render(r.jobs)
}

// ---------------- live updates ----------------

let lastJobs = []
chrome.runtime.onMessage.addListener(msg => {
  if (msg.type === 'jobs') { lastJobs = msg.jobs; render(lastJobs) }
})

async function refresh() {
  const r = await send({ type: 'refresh' })
  if (r && r.jobs) { lastJobs = r.jobs; render(lastJobs) }
}

;(async () => {
  await loadSettings()
  await renderNotion()
  const { jobs } = await chrome.storage.local.get('jobs')
  lastJobs = jobs || []
  render(lastJobs)
  refresh()
  setInterval(refresh, 2000)
  // keeps the retry countdown ticking between polls
  setInterval(() => { if (lastJobs.some(j => j.waiting && !TERMINAL.includes(j.status))) render(lastJobs) }, 1000)
})()
