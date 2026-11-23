// service worker: notion oauth, job submission, status polling, relays updates to the popup.
// state lives in chrome.storage because the service worker gets killed whenever chrome feels like it.

const DEFAULTS = { apiBase: 'http://localhost:8000', parentId: '' }
const TERMINAL = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'DEAD_LETTERED']
const MAX_KEEP = 25

async function getSettings() {
  const s = await chrome.storage.sync.get(['apiBase', 'parentId'])
  return { ...DEFAULTS, ...s, apiBase: (s.apiBase || DEFAULTS.apiBase).replace(/\/+$/, '') }
}

async function getJobs() {
  const { jobs } = await chrome.storage.local.get('jobs')
  return jobs || []
}

async function saveJobs(jobs) {
  await chrome.storage.local.set({ jobs: jobs.slice(0, MAX_KEEP) })
  const active = jobs.filter(j => !TERMINAL.includes(j.status)).length
  chrome.action.setBadgeText({ text: active ? String(active) : '' })
  chrome.action.setBadgeBackgroundColor({ color: '#e52d27' })
  // popup may not be open, that's fine
  chrome.runtime.sendMessage({ type: 'jobs', jobs }).catch(() => {})
}

async function patchJob(id, fields) {
  const jobs = await getJobs()
  const i = jobs.findIndex(j => j.job_id === id)
  if (i === -1) return
  jobs[i] = { ...jobs[i], ...fields, checked_at: Date.now() }
  await saveJobs(jobs)
  return jobs[i]
}

async function api(path, opts = {}) {
  const { apiBase } = await getSettings()
  let res
  try {
    res = await fetch(apiBase + path, {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) }
    })
  } catch (e) {
    throw new Error(`Can't reach the gateway at ${apiBase} - is docker compose up?`)
  }
  const data = await res.json().catch(() => ({}))
  return { ok: res.ok, status: res.status, data }
}

// ---------------- notion oauth ----------------

async function getToken() {
  const { notion } = await chrome.storage.local.get('notion')
  return notion && notion.access_token ? notion : null
}

async function connectNotion() {
  const cfg = await api('/oauth/notion/config')
  if (!cfg.ok) throw new Error(cfg.data.error?.message || 'Gateway has no Notion OAuth config')

  const redirectUri = chrome.identity.getRedirectURL() // https://<extension-id>.chromiumapp.org/
  const state = crypto.randomUUID()
  const url = `${cfg.data.authorize_url}?client_id=${encodeURIComponent(cfg.data.client_id)}` +
    `&response_type=code&owner=user&redirect_uri=${encodeURIComponent(redirectUri)}&state=${state}`

  const back = await chrome.identity.launchWebAuthFlow({ url, interactive: true })
  const q = new URL(back).searchParams
  if (q.get('error')) throw new Error(`Notion said: ${q.get('error')}`)
  if (q.get('state') !== state) throw new Error('OAuth state mismatch, try again')

  const tok = await api('/oauth/notion/token', {
    method: 'POST',
    body: JSON.stringify({ code: q.get('code'), redirect_uri: redirectUri })
  })
  if (!tok.ok) throw new Error(tok.data.error?.message || 'Token exchange failed')

  const notion = {
    access_token: tok.data.access_token,
    workspace_name: tok.data.workspace_name,
    workspace_icon: tok.data.workspace_icon,
    template_page: tok.data.duplicated_template_id || null,
    via: 'oauth',
    at: Date.now()
  }
  await chrome.storage.local.set({ notion })
  return notion
}

async function setManualToken(token) {
  const notion = { access_token: token.trim(), workspace_name: 'Internal integration', via: 'manual', at: Date.now() }
  await chrome.storage.local.set({ notion })
  return notion
}

// ---------------- jobs ----------------

async function submit(videoId, title) {
  const jobs = await getJobs()
  const running = jobs.find(j => j.video_id === videoId && !TERMINAL.includes(j.status))
  if (running) return { ok: true, already: true, job: running }

  // first time: run the oauth flow, after that the stored token is reused
  let notion = await getToken()
  if (!notion) notion = await connectNotion()

  const { parentId } = await getSettings()
  const body = { youtube_url: `https://www.youtube.com/watch?v=${videoId}`, notion_token: notion.access_token }
  // explicit setting wins, otherwise the page notion duplicated from our template during oauth (if any)
  const parent = parentId || notion.template_page
  if (parent) body.notion_parent_id = parent

  const r = await api('/jobs', { method: 'POST', body: JSON.stringify(body) })
  if (!r.ok) throw new Error(r.data.error?.message || `Gateway returned ${r.status}`)

  const job = {
    job_id: r.data.job_id,
    video_id: videoId,
    video_title: title || videoId,
    status: r.data.status,
    progress: 0,
    submitted_at: Date.now()
  }
  await saveJobs([job, ...jobs])
  schedulePoll(500)
  return { ok: true, job }
}

let polling = false
async function pollOnce() {
  if (polling) return
  polling = true
  try {
    const jobs = await getJobs()
    for (const j of jobs.filter(j => !TERMINAL.includes(j.status))) {
      let r
      try {
        r = await api(`/jobs/${j.job_id}`)
      } catch (e) {
        await patchJob(j.job_id, { offline: true })
        continue
      }
      if (r.status === 404) {
        await patchJob(j.job_id, { status: 'FAILED', error: { code: 'LOST', message: 'Gateway has no record of this job any more' } })
        continue
      }
      if (!r.ok) continue
      const d = r.data
      const upd = {
        status: d.status, progress: d.progress, detail: d.detail, waiting: d.waiting,
        warnings: d.warnings, error: d.error, duration_sec: d.duration_sec, offline: false
      }
      if (d.video_title) upd.video_title = d.video_title
      if (TERMINAL.includes(d.status)) {
        const res = await api(`/jobs/${j.job_id}/results`)
        if (res.ok) Object.assign(upd, { result: res.data.result, warnings: res.data.warnings, error: res.data.error })
        notifyDone({ ...j, ...upd })
      }
      await patchJob(j.job_id, upd)
    }
  } finally {
    polling = false
  }
}

function notifyDone(j) {
  const msg = {
    COMPLETED: 'Notes are ready in Notion',
    COMPLETED_WITH_WARNINGS: 'Notes are ready (some frames were skipped)',
    FAILED: j.error?.message || 'Job failed',
    DEAD_LETTERED: 'Gave up after several retries'
  }[j.status]
  chrome.notifications.create(j.job_id, {
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title: j.video_title || 'Video Notes',
    message: msg || j.status
  })
}

chrome.notifications.onClicked.addListener(async id => {
  const j = (await getJobs()).find(x => x.job_id === id)
  if (j?.result?.notion_page_url) chrome.tabs.create({ url: j.result.notion_page_url })
  chrome.notifications.clear(id)
})

// poll every 2s while something is running. the alarm is a backup that wakes the worker up
// again if chrome suspended it in between.
let timer = null
function schedulePoll(ms = 2000) {
  clearTimeout(timer)
  timer = setTimeout(async () => {
    await pollOnce()
    const active = (await getJobs()).some(j => !TERMINAL.includes(j.status))
    if (active) schedulePoll(2000)
  }, ms)
}

chrome.alarms.create('poll', { periodInMinutes: 0.5 })
chrome.alarms.onAlarm.addListener(a => {
  if (a.name === 'poll') schedulePoll(0)
})
chrome.runtime.onStartup.addListener(() => schedulePoll(0))

// ---------------- messages from content script / popup ----------------

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const handlers = {
    submit: () => submit(msg.videoId, msg.title),
    refresh: async () => { await pollOnce(); schedulePoll(2000); return { ok: true, jobs: await getJobs() } },
    connect: async () => ({ ok: true, notion: await connectNotion() }),
    setToken: async () => ({ ok: true, notion: await setManualToken(msg.token) }),
    disconnect: async () => { await chrome.storage.local.remove('notion'); return { ok: true } },
    clearDone: async () => {
      const jobs = (await getJobs()).filter(j => !TERMINAL.includes(j.status))
      await saveJobs(jobs)
      return { ok: true, jobs }
    },
    health: async () => {
      const r = await api('/healthz')
      return { ok: r.ok, data: r.data }
    }
  }
  const fn = handlers[msg.type]
  if (!fn) return false
  fn().then(reply).catch(e => reply({ ok: false, error: e.message || String(e) }))
  return true // async reply
})
