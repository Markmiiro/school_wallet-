# ================================================
# app/routes/account.py
# ------------------------------------------------
# Deleting a parent account. The lifecycle is in app/closures.py.
#
# Parent (app):
#   GET  /account/closure/preview   what will happen, shown before the PIN
#   POST /account/closure           {pin, confirm: "DELETE"}
#
# Parent (web, the Play Store deletion URL — no app needed):
#   GET  /account/delete            the page
#   POST /account/delete/preview    {phone, pin}
#   POST /account/delete            {phone, pin, confirm: "DELETE"}
#
# Operator (super admin only — closures span schools):
#   GET  /account/closures                open closures, with the phone
#   POST /account/closures/{id}/cancel    during the hold only
#   POST /account/closures/process        run what is due now (uncapped)
# ================================================

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import closures
from app.auth import get_current_user, is_super_admin
from app.database import get_db
from app.models import AccountClosure, User
from app.routes.auth import MAX_FAILED_ATTEMPTS, check_pin_or_lock

router = APIRouter()

CONFIRM_WORD = "DELETE"


class ClosureRequest(BaseModel):
    pin: str
    confirm: str = ""


class WebPreviewRequest(BaseModel):
    phone: str
    pin: str


class WebClosureRequest(WebPreviewRequest):
    confirm: str = ""


def _assert_parent(user: User) -> None:
    if user.role != "parent":
        raise HTTPException(
            status_code=403,
            detail="Only parent accounts can be deleted here. Staff accounts "
                   "are removed by the school.",
        )


def _assert_confirmed(word: str) -> None:
    if word != CONFIRM_WORD:
        raise HTTPException(
            status_code=400,
            detail=f'Type {CONFIRM_WORD} in capital letters to confirm.',
        )


def _assert_no_open_closure(db: Session, user: User) -> None:
    if closures.open_closure_for(db, user):
        raise HTTPException(status_code=409, detail="This account is already closing.")


def _accepted(closure: AccountClosure) -> JSONResponse:
    return JSONResponse(status_code=202, content={
        "status": "held",
        "process_after": closure.process_after.isoformat() + "Z",
        "message": (
            f"Your account is closing. You have been signed out. To stop this, "
            f"call {closures.SUPPORT_PHONE} within {closures.HOLD_HOURS} hours."
        ),
    })


# ── App ───────────────────────────────────────────────
@router.get("/closure/preview")
def closure_preview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_parent(current_user)
    return closures.preview(db, current_user)


@router.post("/closure")
def request_closure(
    data: ClosureRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_parent(current_user)
    _assert_no_open_closure(db, current_user)
    _assert_confirmed(data.confirm)
    check_pin_or_lock(db, current_user, data.pin)
    return _accepted(closures.request_closure(db, current_user, via="app"))


# ── Web ───────────────────────────────────────────────
def _normalise_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("+", "").replace("-", "")
    if phone.startswith("0"):
        phone = "256" + phone[1:]
    return phone


def _web_user(db: Session, phone: str, pin: str) -> User:
    user = db.query(User).filter(User.phone == _normalise_phone(phone)).first()
    if not user or user.role != "parent":
        # Same answer as a wrong PIN, as at login: no probing for numbers.
        raise HTTPException(
            status_code=401,
            detail=f"Incorrect PIN. {MAX_FAILED_ATTEMPTS - 1} attempt(s) remaining before lockout.",
        )
    check_pin_or_lock(db, user, pin)
    return user


@router.post("/delete/preview")
def web_preview(data: WebPreviewRequest, db: Session = Depends(get_db)):
    user = _web_user(db, data.phone, data.pin)
    _assert_no_open_closure(db, user)
    return closures.preview(db, user)


@router.post("/delete")
def web_request_closure(data: WebClosureRequest, db: Session = Depends(get_db)):
    _assert_confirmed(data.confirm)
    user = _web_user(db, data.phone, data.pin)
    _assert_no_open_closure(db, user)
    return _accepted(closures.request_closure(db, user, via="web"))


# ── Operator ──────────────────────────────────────────
def _assert_operator(user: User) -> None:
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Operator only.")


@router.get("/closures")
def list_closures(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_operator(current_user)
    rows = (db.query(AccountClosure)
            .filter(AccountClosure.status.in_(closures.OPEN_STATUSES))
            .order_by(AccountClosure.requested_at).all())
    return [{
        "id": c.id, "status": c.status, "requested_via": c.requested_via,
        "refund_phone": c.refund_phone, "refund_amount": c.refund_amount,
        "attempts": c.attempts, "yo_reference": c.yo_reference,
        "requested_at": c.requested_at.isoformat() + "Z",
        "process_after": c.process_after.isoformat() + "Z",
    } for c in rows]


@router.post("/closures/{closure_id}/cancel")
def cancel_closure(
    closure_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_operator(current_user)
    closure = db.get(AccountClosure, closure_id)
    if not closure:
        raise HTTPException(status_code=404, detail="Closure not found.")
    try:
        closures.cancel(db, closure)
    except closures.CancelRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "cancelled"}


@router.post("/closures/process")
async def process_closures(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_operator(current_user)
    return {"results": await closures.process_due(db, automated=False)}


# ── The web page ──────────────────────────────────────
@router.get("/delete", response_class=HTMLResponse)
def deletion_page():
    return _PAGE.replace("{SUPPORT_PHONE}", closures.SUPPORT_PHONE) \
                .replace("{SUPPORT_EMAIL}", closures.SUPPORT_EMAIL)


# Brand tokens from the app (lib/core/constants/app_colors.dart). Teal is a
# fill with a navy label, never text on white; red only for the delete.
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delete your Nuvora account</title>
<style>
  :root { --navy:#0D2551; --ink:#0F1B2E; --slate:#5A6B85; --mist:#EEF2F6;
          --teal:#10C8B0; --teal-text:#0B7A6D; --red:#B42318; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--mist); color:var(--ink); }
  header { background:var(--navy); color:#fff; padding:20px 16px; }
  header h1 { margin:0; font-size:20px; }
  main { max-width:480px; margin:0 auto; padding:16px; }
  .card { background:#fff; border-radius:12px; padding:20px; margin-bottom:16px; }
  label { display:block; font-weight:600; margin:12px 0 6px; }
  input { width:100%; padding:12px; font-size:16px; border:1px solid #C7D0DC;
          border-radius:8px; }
  button { width:100%; padding:14px; font-size:16px; font-weight:700; border:0;
           border-radius:8px; margin-top:16px; cursor:pointer; }
  .primary { background:var(--teal); color:var(--navy); }
  .danger { background:var(--red); color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .muted { color:var(--slate); font-size:14px; }
  .error { color:var(--red); font-weight:600; min-height:1.2em; }
  ul { padding-left:20px; } li { margin:6px 0; }
  .total { font-size:22px; font-weight:700; }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<header><h1>Delete your Nuvora account</h1></header>
<main>
  <section class="card" id="step1">
    <p>Enter the phone number and PIN you use in the Nuvora app. Nothing is
       deleted until you confirm on the next screen.</p>
    <label for="phone">Phone number</label>
    <input id="phone" inputmode="tel" autocomplete="tel" placeholder="07XX XXX XXX">
    <label for="pin">PIN</label>
    <input id="pin" type="password" inputmode="numeric" maxlength="4" autocomplete="off">
    <p class="error" id="err1"></p>
    <button class="primary" id="next">Continue</button>
    <p class="muted">No PIN, or registered by USSD? Call {SUPPORT_PHONE} or email
       {SUPPORT_EMAIL}. We will call you back on your registered number to confirm.</p>
  </section>

  <section class="card" id="step2" hidden>
    <h2>Before you confirm</h2>
    <ul id="children"></ul>
    <p>To be refunded: <span class="total" id="total"></span></p>
    <h3>What happens</h3>
    <ul id="consequences"></ul>
    <label for="confirm">Type DELETE to confirm</label>
    <input id="confirm" autocomplete="off" autocapitalize="characters">
    <p class="error" id="err2"></p>
    <button class="danger" id="go" disabled>Delete my account</button>
  </section>

  <section class="card" id="done" hidden>
    <h2>Your account is closing</h2>
    <p id="doneMsg"></p>
  </section>
</main>
<script>
  const $ = (id) => document.getElementById(id);
  const creds = () => ({ phone: $('phone').value, pin: $('pin').value });
  const ugx = (n) => 'UGX ' + Number(n).toLocaleString('en-UG');
  async function post(path, body) {
    const res = await fetch(path, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = {}; try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, data };
  }
  function item(text) { const li = document.createElement('li'); li.textContent = text; return li; }

  $('next').onclick = async () => {
    $('err1').textContent = ''; $('next').disabled = true;
    const r = await post('delete/preview', creds());
    $('next').disabled = false;
    if (!r.ok) { $('err1').textContent = r.data.detail || 'Something went wrong. Try again.'; return; }
    const p = r.data;
    $('children').replaceChildren(...(p.children.length ? p.children.map(c =>
      item(c.name + ': balance ' + ugx(c.balance) + (c.has_working_card ? ', card stops working' : '')))
      : [item('No children linked to this account.')]));
    if (p.unissued_card_fees) $('children').append(item('Card paid for but not issued: ' + ugx(p.unissued_card_fees)));
    $('total').textContent = ugx(p.refund_total);
    $('consequences').replaceChildren(...p.consequences.map(item));
    $('step1').hidden = true; $('step2').hidden = false;
  };
  $('confirm').oninput = () => { $('go').disabled = $('confirm').value !== 'DELETE'; };
  $('go').onclick = async () => {
    $('err2').textContent = ''; $('go').disabled = true;
    const r = await post('delete', { ...creds(), confirm: $('confirm').value });
    if (!r.ok) { $('err2').textContent = r.data.detail || 'Something went wrong. Try again.';
                 $('go').disabled = false; return; }
    $('doneMsg').textContent = r.data.message;
    $('pin').value = '';
    $('step2').hidden = true; $('done').hidden = false;
  };
</script>
</body>
</html>
"""
