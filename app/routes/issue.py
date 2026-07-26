"""
Admin card console — login, issue NFC cards, look up account numbers.

Mount in main.py:
    from app.routes import issue
    app.include_router(issue.router, prefix="/issue", tags=["Card Issuance"])

Open on an Android phone in Chrome to issue cards:
    https://<host>/issue/
Desktop works for lookup only — Web NFC is Android-Chrome only.

Endpoints used (all already exist):
    POST /auth/login                  -> {phone, pin} JSON, returns {token, user}
    GET  /students/                   -> account_number + nested nfc {tag_uid, status}
    GET  /schools/                    -> school filter
    PUT  /students/{id}/assign-nfc    -> bind a card

SECURITY NOTE: the role check in this page is cosmetic. Hiding a button does not
protect an endpoint. assign-nfc still needs
    current_user: User = Depends(get_current_admin)
on the server, or anyone with curl can reassign any card to any student.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Card Admin — School Wallet</title>
<style>
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
       background:#0f1115;color:#e8eaed;padding-bottom:40px}

  /* ── Login ── */
  .login{position:fixed;inset:0;background:#0f1115;display:flex;flex-direction:column;
         align-items:center;justify-content:center;padding:28px;z-index:50}
  .login.hide{display:none}
  .card{width:100%;max-width:340px}
  .logo{font-size:44px;text-align:center;margin-bottom:10px}
  .login h2{margin:0 0 4px;font-size:21px;text-align:center}
  .login p{margin:0 0 22px;font-size:13.5px;color:#8b93a1;text-align:center}
  .field{margin-bottom:12px}
  .field label{display:block;font-size:12px;color:#8b93a1;margin-bottom:5px}
  .field input{width:100%;padding:13px;font-size:16px;border-radius:9px;
               border:1px solid #262b36;background:#171a21;color:#e8eaed}
  .field input:focus{outline:none;border-color:#00d4aa}
  .btn{width:100%;padding:14px;font-size:16px;font-weight:600;border:0;
       border-radius:9px;background:#00d4aa;color:#06231c;margin-top:6px}
  .btn:disabled{opacity:.5}
  .err{background:#3a1a1a;color:#ff9b9b;padding:11px 13px;border-radius:8px;
       font-size:13.5px;margin-bottom:14px;line-height:1.45;display:none}
  .err.show{display:block}

  /* ── App ── */
  header{padding:14px 16px;background:#171a21;border-bottom:1px solid #262b36;
         position:sticky;top:0;z-index:10}
  .htop{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
  h1{margin:0;font-size:17px}
  .who{font-size:11.5px;color:#8b93a1;margin-top:2px}
  .out{font-size:12px;color:#8b93a1;border:1px solid #262b36;padding:6px 11px;
       border-radius:99px;white-space:nowrap}
  .count{font-size:12.5px;color:#8b93a1;margin-top:6px}
  .bar{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  input[type=text],select{padding:10px;font-size:15px;border-radius:8px;
       border:1px solid #262b36;background:#0f1115;color:#e8eaed;flex:1;min-width:130px}
  .chip{padding:8px 12px;font-size:13px;border-radius:99px;border:1px solid #262b36;
        background:#0f1115;color:#8b93a1;white-space:nowrap}
  .chip.on{background:#0d3b2e;border-color:#00d4aa;color:#00d4aa}
  .wrap{padding:10px 12px}
  .row{padding:13px 14px;margin-top:8px;background:#171a21;border-radius:10px;
       border:1px solid #262b36;display:flex;justify-content:space-between;
       align-items:center;gap:10px}
  .row:active{background:#1e222b}
  .row.issued{opacity:.5}
  .nm{font-weight:600;font-size:15px}
  .acct{font-family:ui-monospace,Menlo,monospace;font-size:13px;color:#00d4aa;
        margin-top:3px;letter-spacing:.5px}
  .sub{font-size:11.5px;color:#8b93a1;margin-top:3px}
  .badge{font-size:10.5px;padding:4px 9px;border-radius:99px;background:#262b36;
         color:#8b93a1;white-space:nowrap}
  .badge.ok{background:#0d3b2e;color:#00d4aa}
  .badge.warn{background:#3a2c10;color:#ffb84d}
  .overlay{position:fixed;inset:0;background:rgba(15,17,21,.97);display:none;
           flex-direction:column;align-items:center;justify-content:center;
           padding:28px;text-align:center;z-index:30}
  .overlay.show{display:flex}
  .big{font-size:62px;margin-bottom:14px}
  .msg{font-size:20px;font-weight:600;margin-bottom:6px}
  .osub{font-size:14px;color:#8b93a1;max-width:320px;line-height:1.5}
  .uid{font-family:ui-monospace,monospace;color:#00d4aa;margin-top:8px;font-size:14px}
  .obtn{margin-top:22px;padding:13px 26px;font-size:15px;border:0;border-radius:8px;
        background:#262b36;color:#e8eaed;font-weight:500}
  .obtn.primary{background:#00d4aa;color:#06231c}
  .warn-box{background:#3a1a1a;color:#ff9b9b;padding:11px 14px;margin:10px 12px;
            border-radius:8px;font-size:13.5px;line-height:1.45}
  .toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
         background:#00d4aa;color:#06231c;padding:10px 18px;border-radius:99px;
         font-size:14px;font-weight:600;opacity:0;transition:opacity .2s;z-index:40}
  .toast.show{opacity:1}
</style>
</head>
<body>

<!-- ══ LOGIN ══ -->
<div class="login" id="login">
  <div class="card">
    <div class="logo">🎒</div>
    <h2>Card Admin</h2>
    <p>School Wallet — staff sign in</p>
    <div class="err" id="loginErr"></div>
    <div class="field">
      <label>Phone number</label>
      <input type="tel" id="phone" placeholder="256700000001" autocomplete="username">
    </div>
    <div class="field">
      <label>PIN</label>
      <input type="password" id="pin" inputmode="numeric" placeholder="••••"
             autocomplete="current-password">
    </div>
    <button class="btn" id="loginBtn" onclick="doLogin()">Sign in</button>
  </div>
</div>

<!-- ══ APP ══ -->
<div id="app" style="display:none">
  <header>
    <div class="htop">
      <div>
        <h1>Card Admin</h1>
        <div class="who" id="who"></div>
      </div>
      <span class="out" onclick="logout()">Sign out</span>
    </div>
    <div class="count" id="count">Loading…</div>
    <div class="bar">
      <input type="text" id="q" placeholder="Name or account number…" autocomplete="off">
      <select id="school"><option value="">All schools</option></select>
      <span class="chip" id="filterChip" onclick="toggleFilter()">No card only</span>
    </div>
  </header>

  <div id="nfcWarn" class="warn-box" style="display:none"></div>
  <div class="wrap"><div id="list"></div></div>
</div>

<div class="overlay" id="overlay">
  <div class="big" id="oIcon">📲</div>
  <div class="msg" id="oMsg"></div>
  <div class="osub" id="oSub"></div>
  <div class="uid" id="oUid"></div>
  <button class="obtn" id="oBtn" onclick="closeOverlay()">Cancel</button>
</div>

<div class="toast" id="toast"></div>

<script>
const API = location.origin;
let TOKEN = null, ME = null;
let students = [], selected = null, scanning = false, onlyNoCard = false;

const esc = s => String(s ?? '').replace(/[<>&"]/g, c =>
  ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const H = () => ({'Authorization':'Bearer ' + TOKEN, 'Content-Type':'application/json'});

function toast(m){
  const t = document.getElementById('toast');
  t.textContent = m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 1600);
}

// ══ LOGIN ═══════════════════════════════════════
function showLoginErr(msg){
  const e = document.getElementById('loginErr');
  e.textContent = msg; e.classList.add('show');
}

async function doLogin(){
  const btn   = document.getElementById('loginBtn');
  const phone = document.getElementById('phone').value.trim();
  const pin   = document.getElementById('pin').value.trim();
  document.getElementById('loginErr').classList.remove('show');

  if (!phone || !pin){ showLoginErr('Enter both phone number and PIN.'); return; }

  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const res = await fetch(API + '/auth/login', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone, pin})
    });
    const data = await res.json().catch(()=>({}));

    if (!res.ok){
      // 429 carries the lockout message; 401 carries attempts remaining
      showLoginErr(data.detail || ('Sign in failed (' + res.status + ')'));
      return;
    }

    // NOTE: the field is "token", not "access_token"
    if (!data.token){ showLoginErr('Server did not return a token.'); return; }

    if (data.user && data.user.role !== 'admin'){
      showLoginErr(`This page is for admins. You are signed in as "${data.user.role}".`);
      return;
    }

    TOKEN = data.token;
    ME    = data.user || null;
    // Session storage, not local — clears when the browser closes, which is
    // what you want on a shared school device.
    sessionStorage.setItem('sw_token', TOKEN);
    sessionStorage.setItem('sw_user', JSON.stringify(ME));
    enterApp();
  } catch(e){
    showLoginErr('Network error — check the connection and try again.');
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
}

document.getElementById('pin').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

function logout(){
  sessionStorage.removeItem('sw_token');
  sessionStorage.removeItem('sw_user');
  location.reload();
}

function enterApp(){
  document.getElementById('login').classList.add('hide');
  document.getElementById('app').style.display = 'block';
  document.getElementById('who').textContent =
    ME ? `${ME.name} · ${ME.role}` : '';
  load();
}

// Token lasts 24h; a stale one just bounces us back to login.
(function restore(){
  const t = sessionStorage.getItem('sw_token');
  if (!t) return;
  TOKEN = t;
  try { ME = JSON.parse(sessionStorage.getItem('sw_user') || 'null'); } catch(e){}
  enterApp();
})();

function sessionExpired(){
  sessionStorage.removeItem('sw_token');
  showOverlay('🔒','Session expired','Sign in again to continue.','','Sign in');
  document.getElementById('oBtn').onclick = () => location.reload();
}

// ══ DATA ════════════════════════════════════════
const uidOf    = s => (s.nfc && s.nfc.tag_uid) || null;
const statusOf = s => (s.nfc && s.nfc.status)  || 'no card slot';

async function load(){
  try {
    const [sRes, schRes] = await Promise.all([
      fetch(API + '/students/', {headers:H()}),
      fetch(API + '/schools/',  {headers:H()}).catch(()=>null)
    ]);
    if (sRes.status === 401){ sessionExpired(); return; }
    if (!sRes.ok) throw new Error('students HTTP ' + sRes.status);
    students = await sRes.json();

    if (schRes && schRes.ok){
      const schools = await schRes.json();
      document.getElementById('school').innerHTML =
        '<option value="">All schools</option>' +
        schools.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    }
    render();
  } catch(e){
    document.getElementById('count').textContent = 'Load failed: ' + e.message;
  }
}

function toggleFilter(){
  onlyNoCard = !onlyNoCard;
  document.getElementById('filterChip').classList.toggle('on', onlyNoCard);
  render();
}

function render(){
  const q      = document.getElementById('q').value.trim().toLowerCase();
  const school = document.getElementById('school').value;
  const issued = students.filter(uidOf).length;

  document.getElementById('count').textContent =
    `${issued} of ${students.length} students have cards`;

  const rows = students
    .filter(s => !school || String(s.school_id) === school)
    .filter(s => !onlyNoCard || !uidOf(s))
    .filter(s => !q
      || (s.name || '').toLowerCase().includes(q)
      || (s.account_number || '').includes(q))
    .sort((a,b) => (uidOf(a)?1:0) - (uidOf(b)?1:0)
                || (a.name||'').localeCompare(b.name||''))
    .map(s => {
      const uid = uidOf(s);
      const badge = uid ? '<span class="badge ok">Issued</span>'
        : statusOf(s) === 'no card slot' ? '<span class="badge warn">No slot</span>'
        : '<span class="badge">Tap to issue</span>';
      return `
      <div class="row ${uid?'issued':''}" onclick="pick(${s.id})">
        <div style="min-width:0">
          <div class="nm">${esc(s.name)}</div>
          <div class="acct" onclick="copyAcct(event,'${esc(s.account_number||'')}')">
            ${esc(s.account_number || 'no account number')}</div>
          <div class="sub">${esc(s.school_name || '-')}${uid ? ' · ' + esc(uid) : ''}</div>
        </div>${badge}
      </div>`;
    }).join('');

  document.getElementById('list').innerHTML =
    rows || '<p style="color:#8b93a1;padding:20px 4px">No students match.</p>';
}

function copyAcct(ev, acct){
  ev.stopPropagation();
  if (!acct) return;
  navigator.clipboard?.writeText(acct).then(()=>toast('Account number copied'));
}

document.getElementById('q').addEventListener('input', render);
document.getElementById('school').addEventListener('change', render);

// ══ ISSUE ═══════════════════════════════════════
function pick(id){
  const s = students.find(x => x.id === id);
  if (!s) return;
  if (statusOf(s) === 'no card slot'){
    showOverlay('!','No card slot',
      `${s.name} has no nfc_tags row, so a card cannot be attached yet.`,'','Close');
    return;
  }
  if (uidOf(s) && !confirm(`${s.name} already has card ${uidOf(s)}.\n\nReplace it?`)) return;
  selected = s;
  showOverlay('📲','Tap the card now', `${s.name} · ${s.account_number || ''}`,'',null);
  startScan();
}

function showOverlay(icon,msg,sub,uid,btn){
  document.getElementById('oIcon').textContent = icon;
  document.getElementById('oMsg').textContent  = msg;
  document.getElementById('oSub').textContent  = sub || '';
  document.getElementById('oUid').textContent  = uid || '';
  const b = document.getElementById('oBtn');
  b.textContent = btn || 'Cancel';
  b.className   = btn ? 'obtn primary' : 'obtn';
  b.onclick     = closeOverlay;
  document.getElementById('overlay').classList.add('show');
}
function closeOverlay(){
  document.getElementById('overlay').classList.remove('show');
  selected = null;
}

async function startScan(){
  if (!('NDEFReader' in window)){
    const w = document.getElementById('nfcWarn');
    w.style.display = 'block';
    w.innerHTML = 'NFC not available in this browser. Issuing cards needs '
      + '<b>Chrome on Android</b> with NFC switched on. '
      + 'Lookup and account numbers still work here.';
    closeOverlay(); return;
  }
  if (scanning) return;
  try {
    const reader = new NDEFReader();
    await reader.scan();
    scanning = true;
    reader.addEventListener('reading', ({serialNumber}) => {
      if (!selected) return;
      assign(selected, serialNumber.replace(/:/g,'').toUpperCase());
    });
  } catch(e){
    const w = document.getElementById('nfcWarn');
    w.style.display = 'block';
    w.textContent = 'NFC permission denied or unavailable: ' + e.message;
    closeOverlay();
  }
}

async function assign(student, uid){
  const target = student;
  selected = null;
  showOverlay('⏳','Saving…', target.name, uid, null);
  try {
    const res = await fetch(
      `${API}/students/${target.id}/assign-nfc?tag_uid=${encodeURIComponent(uid)}`,
      {method:'PUT', headers:H()});
    const data = await res.json().catch(()=>({}));

    if (res.ok){
      target.nfc = {tag_uid: uid, status: 'assigned'};
      render();
      showOverlay('✅','Card issued',
        `${target.name} · ${target.account_number || ''}`, uid, 'Next student');
      navigator.vibrate?.(120);
    } else if (res.status === 401){
      sessionExpired();
    } else if (res.status === 403){
      showOverlay('🔒','Not permitted','Your account is not an admin.','','Close');
    } else {
      showOverlay('❌','Failed', data.detail || ('HTTP ' + res.status), uid, 'Close');
    }
  } catch(e){
    showOverlay('❌','Network error','Card was NOT issued - try again.', uid, 'Close');
  }
}
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def issue_page():
    return HTMLResponse(PAGE)