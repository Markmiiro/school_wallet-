# app/routes/tuckshop.py

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import NFCTag, Wallet, Student

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def tuckshop_interface():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1">
    <title>🏪 Tuck Shop</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

        body {
            font-family: Arial, sans-serif;
            background: #1a1a2e;
            color: white;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        h1 {
            color: #00d4aa;
            font-size: 26px;
            text-align: center;
            margin-bottom: 6px;
        }

        .sub {
            color: #aaa;
            font-size: 14px;
            text-align: center;
            margin-bottom: 30px;
        }

        /* ── LOGIN ── */
        .login-wrap { width: 100%; max-width: 340px; }
        .field { margin-bottom: 14px; text-align: left; }
        .field label {
            display: block; font-size: 12px; color: #aaa; margin-bottom: 5px;
        }
        .field input, .field select {
            width: 100%; padding: 14px; font-size: 16px; border-radius: 10px;
            border: 2px solid #0f3460; background: #0f3460; color: white;
            outline: none;
        }
        .field input:focus, .field select:focus { border-color: #00d4aa; }
        .login-btn {
            width: 100%; padding: 16px; font-size: 17px; font-weight: bold;
            border: none; border-radius: 12px; background: #00d4aa;
            color: #1a1a2e; cursor: pointer; margin-top: 4px;
        }
        .login-btn:disabled { background: #333; color: #666; cursor: not-allowed; }
        .login-err {
            background: #ff444422; border: 1px solid #ff4444; color: #ff9b9b;
            padding: 11px 13px; border-radius: 10px; font-size: 13.5px;
            line-height: 1.45; margin-bottom: 14px; display: none;
        }
        .login-err.show { display: block; }
        .login-note { color: #666; font-size: 12px; text-align: center; margin-top: 16px; }

        /* ── SESSION BAR (shown once signed in) ── */
        .session-bar {
            width: 100%; max-width: 380px; display: flex;
            justify-content: space-between; align-items: center;
            margin-bottom: 4px; font-size: 12px; color: #888;
        }
        .session-bar .who { color: #aaa; }
        .session-bar .signout {
            color: #888; border: 1px solid #333; padding: 5px 10px;
            border-radius: 99px; cursor: pointer;
        }

        /* ── WAITING SCREEN ── */
        .screen { width: 100%; max-width: 380px; display: none; }

        .waiting {
            text-align: center;
            padding: 40px 20px;
        }

        .tap-icon {
            font-size: 90px;
            display: block;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0%   { transform: scale(1);   opacity: 1; }
            50%  { transform: scale(1.1); opacity: 0.6; }
            100% { transform: scale(1);   opacity: 1; }
        }

        .waiting h2 {
            font-size: 22px;
            color: #00d4aa;
            margin-top: 20px;
        }

        .waiting p {
            color: #aaa;
            margin-top: 8px;
            font-size: 15px;
        }

        /* ── STUDENT CARD ── */
        .student-card {
            background: #16213e;
            border-radius: 16px;
            padding: 24px;
            text-align: center;
            margin-bottom: 20px;
            display: none;
        }

        .student-name {
            font-size: 26px;
            font-weight: bold;
            color: white;
        }

        .student-balance {
            font-size: 38px;
            font-weight: bold;
            color: #00d4aa;
            margin-top: 8px;
        }

        .balance-label {
            color: #aaa;
            font-size: 13px;
            margin-top: 4px;
        }

        /* ── AMOUNT ENTRY ── */
        .amount-section {
            background: #16213e;
            border-radius: 16px;
            padding: 24px;
            display: none;
            margin-bottom: 20px;
        }

        .amount-section label {
            display: block;
            color: #aaa;
            font-size: 14px;
            margin-bottom: 10px;
        }

        .amount-input {
            width: 100%;
            padding: 18px;
            border-radius: 12px;
            border: 2px solid #0f3460;
            background: #0f3460;
            color: white;
            font-size: 28px;
            font-weight: bold;
            text-align: center;
            outline: none;
            margin-bottom: 16px;
        }

        .amount-input:focus {
            border-color: #00d4aa;
        }

        /* Quick amount buttons */
        .quick-amounts {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            margin-bottom: 16px;
        }

        .quick-btn {
            padding: 10px 4px;
            border-radius: 8px;
            border: 1px solid #0f3460;
            background: #0f3460;
            color: #aaa;
            font-size: 13px;
            cursor: pointer;
        }

        .quick-btn:active {
            background: #00d4aa33;
            color: #00d4aa;
            border-color: #00d4aa;
        }

        .pay-btn {
            width: 100%;
            padding: 18px;
            border-radius: 12px;
            border: none;
            background: #00d4aa;
            color: #1a1a2e;
            font-size: 20px;
            font-weight: bold;
            cursor: pointer;
        }

        .pay-btn:active { transform: scale(0.98); }
        .pay-btn:disabled {
            background: #333;
            color: #666;
            cursor: not-allowed;
        }

        .cancel-btn {
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            border: 1px solid #333;
            background: transparent;
            color: #aaa;
            font-size: 15px;
            cursor: pointer;
            margin-top: 10px;
        }

        /* ── RESULT ── */
        .result {
            border-radius: 16px;
            padding: 30px;
            text-align: center;
            display: none;
            margin-bottom: 20px;
        }

        .result.success {
            background: #00d4aa22;
            border: 2px solid #00d4aa;
        }

        .result.error {
            background: #ff444422;
            border: 2px solid #ff4444;
        }

        .result.offline {
            background: #ffaa0022;
            border: 2px solid #ffaa00;
        }

        .result-icon  { font-size: 60px; }
        .result-title {
            font-size: 22px;
            font-weight: bold;
            margin: 12px 0;
        }
        .result-detail { color: #ccc; font-size: 16px; line-height: 1.6; }

        .next-btn {
            width: 100%;
            padding: 16px;
            border-radius: 12px;
            border: none;
            background: #0f3460;
            color: white;
            font-size: 17px;
            cursor: pointer;
            margin-top: 20px;
        }

        /* ── NFC NOT SUPPORTED ── */
        .nfc-warning {
            background: #ff444422;
            border: 2px solid #ff4444;
            border-radius: 16px;
            padding: 24px;
            text-align: center;
            display: none;
        }

        .merchant-tag {
            color: #555;
            font-size: 12px;
            text-align: center;
            margin-top: 16px;
        }
    </style>
</head>
<body>

<!-- ══ LOGIN ══ -->
<div id="loginBlock">
    <h1>🏪 School Wallet</h1>
    <p class="sub">Tuck Shop Terminal — staff sign in</p>
    <div class="login-wrap">
        <div class="login-err" id="loginErr"></div>
        <div class="field">
            <label>Phone number</label>
            <input type="tel" id="phone" placeholder="256700000001" autocomplete="username">
        </div>
        <div class="field">
            <label>PIN</label>
            <input type="password" id="pin" inputmode="numeric" placeholder="••••" autocomplete="current-password">
        </div>
        <button class="login-btn" id="loginBtn" onclick="doLogin()">Sign in</button>
        <p class="login-note">Tuck shop staff or admin account.</p>
    </div>
</div>

<!-- ══ MERCHANT PICKER (only shown if a school has more than one tuck shop) ══ -->
<div id="pickerBlock" style="display:none">
    <h1>🏪 School Wallet</h1>
    <p class="sub">Which tuck shop is this device for?</p>
    <div class="login-wrap">
        <div class="login-err" id="pickerErr"></div>
        <div class="field">
            <label>Tuck shop</label>
            <select id="merchantSelect"></select>
        </div>
        <button class="login-btn" onclick="confirmMerchant()">Continue</button>
    </div>
</div>

<!-- ══ APP ══ -->
<div id="appBlock" style="display:none; width:100%; display:flex; flex-direction:column; align-items:center">
    <div class="session-bar">
        <span class="who" id="who"></span>
        <span class="signout" onclick="logout()">Sign out</span>
    </div>

    <h1>🏪 School Wallet</h1>
    <p class="sub">Tuck Shop Terminal</p>

    <div class="screen" id="mainScreen" style="display:block">

        <!-- NFC not supported -->
        <div class="nfc-warning" id="nfcWarning">
            <p style="font-size:40px">📵</p>
            <p style="font-weight:bold; margin-top:10px">NFC Not Available</p>
            <p style="color:#aaa; font-size:14px; margin-top:8px">
                Please use an Android phone with NFC enabled
                and open this page in Chrome browser.
            </p>
        </div>

        <!-- Waiting for tap -->
        <div class="waiting" id="waiting">
            <span class="tap-icon">📡</span>
            <h2>Ready for Payment</h2>
            <p>Ask student to tap their bracelet</p>
        </div>

        <!-- Student card -->
        <div class="student-card" id="studentCard">
            <div class="student-name"    id="studentName">-</div>
            <div class="student-balance" id="studentBalance">UGX 0</div>
            <div class="balance-label">Available balance</div>
        </div>

        <!-- Amount entry -->
        <div class="amount-section" id="amountSection">
            <label>💰 Enter amount (UGX):</label>

            <!-- Quick amount shortcuts -->
            <div class="quick-amounts">
                <button class="quick-btn" onclick="setAmount(500)">500</button>
                <button class="quick-btn" onclick="setAmount(1000)">1,000</button>
                <button class="quick-btn" onclick="setAmount(1500)">1,500</button>
                <button class="quick-btn" onclick="setAmount(2000)">2,000</button>
                <button class="quick-btn" onclick="setAmount(2500)">2,500</button>
                <button class="quick-btn" onclick="setAmount(3000)">3,000</button>
                <button class="quick-btn" onclick="setAmount(4000)">4,000</button>
                <button class="quick-btn" onclick="setAmount(5000)">5,000</button>
            </div>

            <!-- Or type custom amount -->
            <input
                type="number"
                id="amountInput"
                class="amount-input"
                placeholder="0"
                min="100"
                inputmode="numeric"
            />

            <button class="pay-btn" id="payBtn" onclick="processPayment()">
                💳 Charge
            </button>

            <button class="cancel-btn" onclick="cancelPayment()">
                ✕ Cancel
            </button>
        </div>

        <!-- Result -->
        <div class="result" id="result">
            <div class="result-icon"   id="resultIcon">✅</div>
            <div class="result-title"  id="resultTitle">Done!</div>
            <div class="result-detail" id="resultDetail"></div>
            <button class="next-btn" onclick="resetForNext()">
                Next Student →
            </button>
        </div>

        <div class="merchant-tag" id="merchantTag">Loading...</div>

    </div>
</div>

<script>
const API_BASE = window.location.origin;

let TOKEN = null, ME = null, MERCHANT_ID = null, MERCHANT_NAME = null;
let currentTagUid = null;
let currentRequestId = null;   // one UUID per tap; sent with the charge so a
                                // retry of this exact attempt doesn't double-charge

const authHeaders = () => ({ 'Authorization': 'Bearer ' + TOKEN });

// ══ LOGIN ═══════════════════════════════════════
function showLoginErr(msg) {
    const e = document.getElementById('loginErr');
    e.textContent = msg;
    e.classList.add('show');
}

async function doLogin() {
    const btn   = document.getElementById('loginBtn');
    const phone = document.getElementById('phone').value.trim();
    const pin   = document.getElementById('pin').value.trim();
    document.getElementById('loginErr').classList.remove('show');

    if (!phone || !pin) { showLoginErr('Enter both phone number and PIN.'); return; }

    btn.disabled = true; btn.textContent = 'Signing in…';
    try {
        const res = await fetch(API_BASE + '/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone, pin })
        });
        const data = await res.json().catch(() => ({}));

        if (!res.ok) {
            showLoginErr(data.detail || ('Sign in failed (' + res.status + ')'));
            return;
        }
        if (!data.token) { showLoginErr('Server did not return a token.'); return; }

        // Cosmetic only — the money routes enforce their own auth
        // server-side regardless of what this page shows.
        if (data.user && !['merchant', 'admin'].includes(data.user.role)) {
            showLoginErr(`This device is for tuck shop staff. You are signed in as "${data.user.role}".`);
            return;
        }

        TOKEN = data.token;
        ME    = data.user || null;
        sessionStorage.setItem('tsw_token', TOKEN);
        sessionStorage.setItem('tsw_user', JSON.stringify(ME));
        sessionStorage.removeItem('tsw_merchant'); // fresh login re-picks the tuck shop
        await afterLogin();
    } catch (e) {
        showLoginErr('Network error — check the connection and try again.');
    } finally {
        btn.disabled = false; btn.textContent = 'Sign in';
    }
}

document.getElementById('pin').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
});

function logout() {
    sessionStorage.removeItem('tsw_token');
    sessionStorage.removeItem('tsw_user');
    sessionStorage.removeItem('tsw_merchant');
    location.reload();
}

function sessionExpired() {
    sessionStorage.removeItem('tsw_token');
    TOKEN = null;
    document.getElementById('appBlock').style.display = 'none';
    document.getElementById('pickerBlock').style.display = 'none';
    document.getElementById('loginBlock').style.display = 'block';
    showLoginErr('Session expired — sign in again to continue.');
}

// Token lasts 24h; a stale one just bounces back to login on first 401.
(function restore() {
    const t = sessionStorage.getItem('tsw_token');
    if (!t) return;
    TOKEN = t;
    try { ME = JSON.parse(sessionStorage.getItem('tsw_user') || 'null'); } catch (e) {}
    afterLogin();
})();

// ══ MERCHANT (TUCK SHOP) SELECTION ═══════════════
async function afterLogin() {
    document.getElementById('loginBlock').style.display = 'none';

    const savedMerchant = sessionStorage.getItem('tsw_merchant');
    if (savedMerchant) {
        const m = JSON.parse(savedMerchant);
        MERCHANT_ID = m.id;
        MERCHANT_NAME = m.name;
        enterApp();
        return;
    }

    if (!ME || !ME.school_id) {
        document.getElementById('pickerBlock').style.display = 'block';
        document.getElementById('pickerErr').textContent =
            'Your account has no school attached — ask an admin to fix this before using the till.';
        document.getElementById('pickerErr').classList.add('show');
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/merchants/school/${ME.school_id}`, { headers: authHeaders() });
        if (res.status === 401) { sessionExpired(); return; }
        const data = await res.json();
        const merchants = (data.merchants || []).map(m => ({ ...m, school_id: ME.school_id }));

        if (merchants.length === 0) {
            document.getElementById('pickerBlock').style.display = 'block';
            document.getElementById('pickerErr').textContent =
                'No tuck shop is set up for your school yet — ask an admin to add one.';
            document.getElementById('pickerErr').classList.add('show');
            return;
        }

        if (merchants.length === 1) {
            MERCHANT_ID = merchants[0].id;
            MERCHANT_NAME = merchants[0].name;
            sessionStorage.setItem('tsw_merchant', JSON.stringify(merchants[0]));
            enterApp();
            return;
        }

        // More than one tuck shop at this school — ask which one this device is for.
        const select = document.getElementById('merchantSelect');
        select.innerHTML = merchants.map(m => `<option value="${m.id}">${escapeHtml(m.name)}</option>`).join('');
        select.dataset.merchants = JSON.stringify(merchants);
        document.getElementById('pickerBlock').style.display = 'block';
    } catch (e) {
        document.getElementById('pickerBlock').style.display = 'block';
        document.getElementById('pickerErr').textContent = 'Could not load tuck shops — check the connection.';
        document.getElementById('pickerErr').classList.add('show');
    }
}

function confirmMerchant() {
    const select = document.getElementById('merchantSelect');
    const merchants = JSON.parse(select.dataset.merchants || '[]');
    const picked = merchants.find(m => String(m.id) === select.value);
    if (!picked) return;
    MERCHANT_ID = picked.id;
    MERCHANT_NAME = picked.name;
    sessionStorage.setItem('tsw_merchant', JSON.stringify(picked));
    document.getElementById('pickerBlock').style.display = 'none';
    enterApp();
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

// ══ APP ═══════════════════════════════════════════
function enterApp() {
    document.getElementById('appBlock').style.display = 'flex';
    document.getElementById('who').textContent = ME ? `${ME.name} · ${ME.role}` : '';
    document.getElementById('merchantTag').textContent = `🏪 ${MERCHANT_NAME || 'Tuck Shop'}`;
    startNFC();
}

// ── Start NFC ────────────────────────────────────
async function startNFC() {
    if (!('NDEFReader' in window)) {
        document.getElementById('waiting').style.display    = 'none';
        document.getElementById('nfcWarning').style.display = 'block';
        return;
    }

    try {
        const reader = new NDEFReader();
        await reader.scan();

        reader.addEventListener('reading', ({ serialNumber }) => {
            const uid = serialNumber.replace(/:/g, '').toUpperCase();
            onStudentTap(uid);
        });

    } catch(e) {
        document.getElementById('waiting').style.display    = 'none';
        document.getElementById('nfcWarning').style.display = 'block';
    }
}

// ── Student taps bracelet ────────────────────────
async function onStudentTap(uid) {
    currentTagUid = uid;
    currentRequestId = crypto.randomUUID();

    // Show loading
    document.getElementById('waiting').innerHTML =
        '<span style="font-size:60px">⏳</span>' +
        '<h2 style="margin-top:16px; color:#00d4aa">Loading...</h2>';

    try {
        const res  = await fetch(`${API_BASE}/tuckshop/check?tag_uid=${uid}`);
        const data = await res.json();

        if (res.ok) {
            // Show student info
            document.getElementById('studentName').textContent =
                data.student_name;
            document.getElementById('studentBalance').textContent =
                `UGX ${data.balance.toLocaleString()}`;

            show('studentCard');
            show('amountSection');
            hide('waiting');
            hide('result');

            // Focus amount input for fast entry
            document.getElementById('amountInput').value = '';
            document.getElementById('amountInput').focus();

        } else {
            alert(data.detail || 'Bracelet not registered. Contact admin.');
            resetWaiting();
        }

    } catch(e) {
        alert('No internet connection.');
        resetWaiting();
    }
}

// ── Quick amount shortcut ────────────────────────
function setAmount(amount) {
    document.getElementById('amountInput').value = amount;
    document.getElementById('amountInput').focus();
}

// ── Process payment ──────────────────────────────
async function processPayment() {
    const amount = parseInt(document.getElementById('amountInput').value);

    if (!amount || amount < 100) {
        alert('Please enter a valid amount');
        return;
    }

    const payBtn = document.getElementById('payBtn');
    payBtn.disabled    = true;
    payBtn.textContent = 'Processing...';

    try {
        const res = await fetch(
            `${API_BASE}/payments/nfc?tag_uid=${currentTagUid}` +
            `&merchant_id=${MERCHANT_ID}&amount=${amount}` +
            `&description=Tuck shop purchase&request_id=${currentRequestId}`,
            { method: 'POST', headers: authHeaders() }
        );

        if (res.status === 401) {
            sessionExpired();
            payBtn.disabled = false; payBtn.textContent = '💳 Charge';
            return;
        }

        const data = await res.json();

        hide('studentCard');
        hide('amountSection');
        show('result');

        if (res.ok) {
            document.getElementById('result').className =
                'result success';
            document.getElementById('resultIcon').textContent =
                '✅';
            document.getElementById('resultTitle').textContent =
                'Payment Done!';
            document.getElementById('resultDetail').innerHTML =
                `<strong>UGX ${amount.toLocaleString()}</strong> charged<br>
                 Balance left:
                 <strong>UGX ${data.remaining_balance.toLocaleString()}</strong>`;
        } else {
            document.getElementById('result').className =
                'result error';
            document.getElementById('resultIcon').textContent  = '❌';
            document.getElementById('resultTitle').textContent = 'Failed';
            document.getElementById('resultDetail').textContent =
                data.detail || 'Payment failed. Try again.';
        }

    } catch(e) {
        // Save offline
        saveOffline(currentTagUid, amount);
        hide('studentCard');
        hide('amountSection');
        show('result');
        document.getElementById('result').className =
            'result offline';
        document.getElementById('resultIcon').textContent  = '💾';
        document.getElementById('resultTitle').textContent = 'Saved Offline';
        document.getElementById('resultDetail').textContent =
            `UGX ${amount.toLocaleString()} saved.
             Will sync when internet returns.`;
    }

    payBtn.disabled    = false;
    payBtn.textContent = '💳 Charge';
}

// ── Cancel ───────────────────────────────────────
function cancelPayment() {
    currentTagUid = null;
    hide('studentCard');
    hide('amountSection');
    resetWaiting();
}

// ── Save offline ──────────────────────────────────
function saveOffline(tagUid, amount) {
    const q = JSON.parse(
        localStorage.getItem('offlinePayments') || '[]'
    );
    q.push({
        tag_uid:   tagUid,
        amount:    amount,
        timestamp: new Date().toISOString()
    });
    localStorage.setItem('offlinePayments', JSON.stringify(q));
}

// ── Reset ─────────────────────────────────────────
function resetForNext() {
    currentTagUid = null;
    hide('studentCard');
    hide('amountSection');
    hide('result');
    resetWaiting();
}

function resetWaiting() {
    show('waiting');
    document.getElementById('waiting').innerHTML =
        '<span class="tap-icon">📡</span>' +
        '<h2>Ready for Payment</h2>' +
        '<p>Ask student to tap their bracelet</p>';
}

function show(id) {
    document.getElementById(id).style.display = 'block';
}
function hide(id) {
    document.getElementById(id).style.display = 'none';
}
</script>

</body>
</html>
"""


@router.get("/check")
def check_nfc_tag(tag_uid: str, db: Session = Depends(get_db)):
    """Check student info from NFC tag UID."""
    nfc = db.query(NFCTag).filter(NFCTag.tag_uid == tag_uid).first()
    if not nfc:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Bracelet not registered. Contact admin."}
        )

    wallet = db.query(Wallet).filter(
        Wallet.student_id == nfc.student_id
    ).first()
    if not wallet:
        return JSONResponse(
            status_code=404,
            content={"detail": "Wallet not found"}
        )

    if not wallet.is_active:
        return JSONResponse(
            status_code=403,
            content={"detail": "This wallet is deactivated"}
        )

    student = db.query(Student).filter(
        Student.id == nfc.student_id
    ).first()

    return JSONResponse(content={
        "tag_uid":      tag_uid,
        "student_name": student.name if student else "Unknown",
        "student_id":   nfc.student_id,
        "wallet_id":    wallet.id,
        "balance":      wallet.balance,
        "is_active":    wallet.is_active,
    })
