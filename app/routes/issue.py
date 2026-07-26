"""
Card issuance page — bulk-assign physical NFC cards to students.

Mount in main.py:
    from app.routes import issue
    app.include_router(issue.router, prefix="/issue", tags=["Card Issuance"])

Open on an Android phone in Chrome:
    https://<your-host>/issue/

Uses only existing endpoints:
    GET  /students/                        → list students
    PUT  /students/{id}/assign-nfc         → bind a card

Web NFC requires Chrome on Android over HTTPS. It will not work on iPhone,
Firefox, or desktop.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Issue Cards</title>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; font-family:system-ui,-apple-system,sans-serif;
         background:#12151a; color:#e8eaed; }
  header { padding:16px; background:#1b1f27; border-bottom:1px solid #2a2f3a;
           position:sticky; top:0; z-index:10; }
  h1 { margin:0; font-size:18px; }
  .count { font-size:13px; color:#8b93a1; margin-top:4px; }
  .wrap { padding:12px; }
  input[type=text] { width:100%; padding:12px; font-size:16px; border-radius:8px;
                     border:1px solid #2a2f3a; background:#1b1f27; color:#e8eaed; }
  .student { padding:14px; margin-top:8px; background:#1b1f27; border-radius:8px;
             border:1px solid #2a2f3a; display:flex; justify-content:space-between;
             align-items:center; }
  .student:active { background:#232833; }
  .name { font-weight:600; }
  .meta { font-size:12px; color:#8b93a1; margin-top:2px; }
  .done { opacity:.45; }
  .badge { font-size:11px; padding:3px 8px; border-radius:99px;
           background:#2a2f3a; color:#8b93a1; }
  .badge.ok { background:#0d3b2e; color:#00d4aa; }
  .overlay { position:fixed; inset:0; background:#12151aF2; display:none;
             flex-direction:column; align-items:center; justify-content:center;
             padding:24px; text-align:center; z-index:20; }
  .overlay.show { display:flex; }
  .big { font-size:64px; margin-bottom:12px; }
  .msg { font-size:20px; font-weight:600; margin-bottom:8px; }
  .sub { font-size:14px; color:#8b93a1; }
  button { margin-top:20px; padding:14px 28px; font-size:16px; border:0;
           border-radius:8px; background:#2a2f3a; color:#e8eaed; }
  button.primary { background:#00d4aa; color:#06231c; font-weight:600; }
  .warn { background:#3b1d1d; color:#ff8b8b; padding:12px; border-radius:8px;
          margin:12px; font-size:14px; }
</style>
</head>
<body>

<header>
  <h1>Issue Cards</h1>
  <div class="count" id="count">Loading…</div>
</header>

<div id="nfcWarn" class="warn" style="display:none">
  NFC not available. Use <b>Chrome on Android</b> with NFC switched on.
</div>

<div class="wrap">
  <input type="text" id="search" placeholder="Search student name…" autocomplete="off">
  <div id="list"></div>
</div>

<div class="overlay" id="overlay">
  <div class="big" id="oIcon">📲</div>
  <div class="msg" id="oMsg"></div>
  <div class="sub" id="oSub"></div>
  <button id="oBtn" onclick="closeOverlay()">Cancel</button>
</div>

<script>
const API = window.location.origin;
let students = [], selected = null, scanning = false;

// ── Admin token (assign-nfc should be admin-protected) ──
let TOKEN = localStorage.getItem('sw_token');
if (!TOKEN) {
  TOKEN = prompt('Paste admin access token (from /auth/login):') || '';
  if (TOKEN) localStorage.setItem('sw_token', TOKEN);
}
const authHeaders = TOKEN ? { 'Authorization': 'Bearer ' + TOKEN } : {};

function esc(s){ return String(s??'').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])); }

async function load() {
  try {
    const res = await fetch(API + '/students/', { headers: authHeaders });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    students = Array.isArray(data) ? data : (data.students || []);
    render();
  } catch (e) {
    document.getElementById('count').textContent = 'Could not load students: ' + e.message;
  }
}

function render() {
  const q = document.getElementById('search').value.toLowerCase();
  const withCard = students.filter(s => s.tag_uid).length;
  document.getElementById('count').textContent =
    withCard + ' of ' + students.length + ' students have cards';

  const rows = students
    .filter(s => (s.name || '').toLowerCase().includes(q))
    .sort((a, b) => (a.tag_uid ? 1 : 0) - (b.tag_uid ? 1 : 0))
    .map(s => `
      <div class="student ${s.tag_uid ? 'done' : ''}" onclick="pick(${s.id})">
        <div>
          <div class="name">${esc(s.name)}</div>
          <div class="meta">${s.tag_uid ? esc(s.tag_uid) : 'No card yet'}</div>
        </div>
        <span class="badge ${s.tag_uid ? 'ok' : ''}">${s.tag_uid ? 'Issued' : 'Tap to issue'}</span>
      </div>`).join('');

  document.getElementById('list').innerHTML = rows || '<p style="color:#8b93a1">No matches.</p>';
}

document.getElementById('search').addEventListener('input', render);

function pick(id) {
  const s = students.find(x => x.id === id);
  if (!s) return;
  if (s.tag_uid && !confirm(`${s.name} already has a card.\\nReplace it?`)) return;
  selected = s;
  showOverlay('📲', 'Tap the card now', 'Hold a blank card to the back of the phone');
  startScan();
}

function showOverlay(icon, msg, sub, btn) {
  document.getElementById('oIcon').textContent = icon;
  document.getElementById('oMsg').textContent  = msg;
  document.getElementById('oSub').textContent  = sub || '';
  const b = document.getElementById('oBtn');
  b.textContent = btn || 'Cancel';
  b.className   = btn ? 'primary' : '';
  document.getElementById('overlay').classList.add('show');
}
function closeOverlay() {
  document.getElementById('overlay').classList.remove('show');
  selected = null;
}

async function startScan() {
  if (!('NDEFReader' in window)) {
    document.getElementById('nfcWarn').style.display = 'block';
    closeOverlay();
    return;
  }
  if (scanning) return;
  try {
    const reader = new NDEFReader();
    await reader.scan();
    scanning = true;
    reader.addEventListener('reading', ({ serialNumber }) => {
      if (!selected) return;                         // ignore stray taps
      const uid = serialNumber.replace(/:/g, '').toUpperCase();
      assign(selected, uid);
    });
  } catch (e) {
    document.getElementById('nfcWarn').style.display = 'block';
    closeOverlay();
  }
}

async function assign(student, uid) {
  const target = student;
  selected = null;                                   // prevent double-fire
  showOverlay('⏳', 'Saving…', uid);
  try {
    const res = await fetch(
      `${API}/students/${target.id}/assign-nfc?tag_uid=${encodeURIComponent(uid)}`,
      { method: 'PUT', headers: authHeaders }
    );
    const data = await res.json().catch(() => ({}));

    if (res.ok) {
      target.tag_uid = uid;
      render();
      showOverlay('✅', 'Card issued', `${target.name} → ${uid}`, 'Next student');
      if (navigator.vibrate) navigator.vibrate(120);
    } else if (res.status === 401 || res.status === 403) {
      localStorage.removeItem('sw_token');
      showOverlay('🔒', 'Not authorised', 'Reload and paste a valid admin token', 'Close');
    } else {
      showOverlay('❌', 'Failed', data.detail || ('HTTP ' + res.status), 'Close');
    }
  } catch (e) {
    showOverlay('❌', 'Network error', 'Card NOT issued — try again', 'Close');
  }
}

load();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def issue_page():
    return HTMLResponse(PAGE)