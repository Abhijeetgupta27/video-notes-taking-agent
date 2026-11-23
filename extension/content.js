// injects the "Take Notes" button into the youtube player controls.
// youtube is a SPA, so the page never really reloads when you click another video -
// we re-check on yt-navigate-finish and also with a (throttled) MutationObserver.

const BTN_ID = 'vna-take-notes'

function currentVideoId() {
  const u = new URL(location.href)
  if (u.pathname !== '/watch') return null
  const v = u.searchParams.get('v')
  return /^[A-Za-z0-9_-]{11}$/.test(v || '') ? v : null
}

function findToolbar() {
  const player = document.querySelector('#movie_player') || document.querySelector('.html5-video-player')
  if (!player) return null
  // newer player layout splits the right side into two groups, older one doesn't
  return player.querySelector('.ytp-right-controls-left') || player.querySelector('.ytp-right-controls')
}

const ICON = `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
  <path fill="currentColor" d="M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 1.5V8h4.5L14 3.5zM7 11v1.6h10V11H7zm0 3.4V16h10v-1.6H7zm0 3.4v1.6h6.5v-1.6H7z"/>
</svg>`

function setLabel(btn, text, state) {
  btn.querySelector('.vna-label').textContent = text
  btn.dataset.state = state || ''
}

function makeButton() {
  const b = document.createElement('button')
  b.id = BTN_ID
  b.className = 'ytp-button vna-btn'
  b.title = 'Take Notes (send this video to Notion)'
  b.innerHTML = ICON + '<span class="vna-label">Take Notes</span>'
  b.addEventListener('click', onClick, true)
  return b
}

function inject() {
  const vid = currentVideoId()
  const existing = document.getElementById(BTN_ID)
  if (!vid) {
    if (existing) existing.remove()
    return
  }
  if (existing) {
    // video changed under us -> reset the label from the previous video
    if (existing.dataset.vid !== vid) {
      existing.dataset.vid = vid
      setLabel(existing, 'Take Notes')
    }
    return
  }
  const bar = findToolbar()
  if (!bar) return
  const b = makeButton()
  b.dataset.vid = vid
  bar.prepend(b)
}

async function onClick(e) {
  e.preventDefault()
  e.stopPropagation()
  const btn = e.currentTarget
  // read the id at click time, never cache it - user may have navigated since the button was added
  const vid = currentVideoId()
  if (!vid || btn.dataset.state === 'busy') return

  const title = (document.querySelector('h1.ytd-watch-metadata')?.textContent || document.title)
    .replace(/\s*-\s*YouTube\s*$/, '').trim()

  setLabel(btn, 'Sending…', 'busy')
  let res
  try {
    res = await chrome.runtime.sendMessage({ type: 'submit', videoId: vid, title })
  } catch (err) {
    // happens after the extension is reloaded but the tab isn't
    setLabel(btn, 'Reload page', 'error')
    return
  }
  if (currentVideoId() !== vid) return // navigated away meanwhile
  if (res && res.ok) {
    setLabel(btn, res.already ? 'Already running' : 'Queued ✓', 'ok')
  } else {
    setLabel(btn, 'Failed', 'error')
    btn.title = (res && res.error) || 'Something went wrong'
  }
  setTimeout(() => {
    if (btn.isConnected && currentVideoId() === vid) {
      setLabel(btn, 'Take Notes')
      btn.title = 'Take Notes (send this video to Notion)'
    }
  }, 4000)
}

// --- keep it alive across navigation

document.addEventListener('yt-navigate-finish', () => setTimeout(inject, 50))
window.addEventListener('popstate', () => setTimeout(inject, 50))

let pending = false
new MutationObserver(() => {
  if (pending) return
  pending = true
  setTimeout(() => { pending = false; inject() }, 400)
}).observe(document.documentElement, { childList: true, subtree: true })

inject()
