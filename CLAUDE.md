# CLAUDE.md — School Wallet

Project context and working rules for Claude Code. Read this fully before doing
anything. When in doubt, ask before acting — this system moves real money.

---

## What this project is

**School Wallet** (Miiro Technologies) is a Uganda school-fees / student-spending
platform. Parents fund a wallet tied to a student; the student spends from it at a
school tuck shop by tapping an NFC card; merchants get paid out their sales. Money
moves through **Yo Uganda Limited** (a Bank-of-Uganda-licensed payments gateway).

- Backend: FastAPI + SQLAlchemy + PostgreSQL, deployed on Railway.
- Live URL: https://web-production-454a5.up.railway.app
- Frontend: Flutter app (parents), plus tuck-shop and admin UIs.

---

## ⚠️ Working rules — read carefully

### You MAY:
- **Build UI** — the Flutter app, the tuck-shop interface, and the admin UI.
  Create and edit files for these frontends.
- **Write tests** — unit tests, integration tests, and stress/load tests for the
  whole system. Create new test files freely (e.g. under `tests/`).
- **Read any file** in the repo to understand how things work.
- **Run tests** and report results.

### You MUST get my explicit approval before:
- Any UI change — show me the plan/diff and wait for me to say go. "With my
  supervision" means I review before you apply. Do not batch many UI edits and
  apply them silently.
- Installing new dependencies (Flutter packages, Python test libs, etc.).
- Running anything that hits the **live** Railway database or deploys.

### You MUST NEVER (hard rules, no exceptions):
- **Never edit `app/routes/ussd.py`.** It is stamped and contractually APPROVED by
  Yo Uganda (11/7/26). Changing it can break the agreement and require re-approval.
  You may READ it to write tests, never modify it.
- **Never edit the money-movement backend logic to make a test pass.** That means
  `app/momo.py`, `app/routes/webhook.py`, and `app/auth.py`. If a test reveals a
  bug in these, STOP and report it to me with the details — do not "fix" it
  yourself. I decide how money code changes.
- **Never touch secrets or keys.** Do not print, move, commit, or regenerate
  `private_key.pem`, `.env` contents, `YO_PRIVATE_KEY`, `YO_PASSWORD`, or any
  credential. `private_key.pem` and `*.pem` are gitignored — keep it that way.
- **Never commit or push** unless I explicitly ask. Show me changes; I commit.
- **Never run destructive DB commands** (DROP, DELETE, UPDATE) against any real
  database. Tests must use a separate throwaway/test database, never production.

If a task seems to require breaking one of these rules, stop and ask me instead.

---

## Architecture (so you don't rediscover it)

Backend lives under `app/`:
- `app/models.py` — SQLAlchemy models: User, School, Student, Wallet, Transaction,
  NFCTag, Merchant, Payment.
- `app/auth.py` — JWT + PIN hashing utilities (hash_pin, verify_pin,
  create_access_token, get_current_user, get_current_admin). This file DEFINES
  these; `app/routes/auth.py` imports FROM it. Do not merge the two — that mistake
  has happened before and broke the app.
- `app/account_number.py` — generates 12-digit student account numbers.
- `app/momo.py` — Yo Uganda payment calls (charge_mobile_money, verify_transaction,
  disburse_to_merchant). XML over HTTP. **Money code — read-only for you.**
- `app/routes/` — FastAPI routers: auth, students, schools, merchants, topup,
  tuckshop, payments, wallets, ussd, webhook, users, reports, analytics.
- `app/routes/webhook.py` — receives Yo Uganda callbacks. **Money code — read-only.**
- `app/routes/ussd.py` — **FROZEN. Never edit.**

Key domain facts:
- Money amounts are in UGX.
- `Transaction` rows are append-only: never delete, never mutate amount.
- Wallet must never go negative; a daily_limit (default 20,000 UGX) caps spending.
- Reference formats routed by webhook.py: `USSD-TOPUP-{id}-{amount}-{uuid8}`,
  `USSD-REG-{uuid8}`, or a plain UUID.

---

## Test mode (important for testing)

The backend fakes all Yo Uganda calls when `YO_USERNAME` is unset OR
`APP_ENV != "production"` (see `_is_test_mode()` in `app/momo.py`). Tests should
rely on this / mock Yo rather than hitting the real gateway. A real Yo IPN
callback carries an RSA signature that cannot be faked — so webhook tests must
mock the signature-verification layer, not try to forge a signature.

Yo signature verification uses Yo's public cert in `app/certs/` (sandbox cert
active unless production). Outbound withdraw signing uses OUR private key
(`YO_PRIVATE_KEY`) — do not touch the key material in tests; mock the signer.

---

## What to prioritise (money-critical, test these hardest)

The highest-value tests, roughly in order:
1. **Tap-to-pay / tuck-shop payment loop** — debits the correct wallet and amount,
   cannot go negative, respects daily_limit, cannot double-charge on retries.
2. **Wallet crediting via webhook** — correct reference routing (TOPUP vs REG vs
   UUID), idempotency (a repeated callback must not double-credit), amount matches.
3. **Auth + rate-limiting** — 5 failed PINs → lockout, correct PIN resets counter.
4. **USSD-REG registration** — creates Student + Wallet + NFCTag + parent User
   exactly once, deletes the pending row. (Test against `app/routes/webhook.py`'s
   handler; do NOT modify ussd.py.)
5. **Withdraw signing** — `sign_withdraw_request` produces a stable, correctly
   ordered signature (mock the private key).

For stress testing: focus on concurrency around wallet balance (two payments at
once must not let the balance go negative or double-spend) and webhook idempotency
under duplicate/rapid callbacks. Use a dedicated test database.

---

## Known open issues (context, not tasks unless I say so)

- Tap-to-pay loop is unproven end-to-end — verifying it is high value.
- USSD-REG handler built but never run against a real payment.
- Withdraw signing awaiting Yo enabling our public key on sandbox.
- Two near-duplicate school rows exist (ids 1 and 2).
- Near-zero automated tests currently exist — that's the main gap you're helping close.
- Live `payments` table is missing a `timestamp` column that `Payment` (app/models.py)
  declares — found 2026-07-31 while auditing `payments.reference` for duplicates.
  `create_all()` never retroactively alters an existing table, so the live table
  predates that field. Needs reconciliation (add the column live) — not done yet,
  flagged only.
- `app/routes/reports.py`'s `automated_daily_payout` falls back to a hardcoded
  default (`"school_wallet_settle_2026"`) for `SETTLEMENT_SECRET` if that env var
  isn't set on Railway — found 2026-07-31 while fixing the payout double-charge
  race. That default is now in git history, so it should be treated as public.
  Confirm `SETTLEMENT_SECRET` is actually set in Railway → Variables; if it's ever
  relying on the fallback, rotate it. Flagged only, not changed.
- **Mixed date basis: UTC timestamps compared against local-clock dates.** Found
  2026-08-22. Every `Transaction.timestamp` / `Payment.timestamp` is written with
  `datetime.utcnow()` (UTC), but the dates they are compared against come from
  `date.today()`, which is the *app container's local* date. There is no timezone
  handling anywhere in `app/` — no `ZoneInfo`, no `pytz`, no `astimezone`. Two
  places matter:
    * `app/routes/reports.py:375` — `_process_merchant_payout` selects a day's
      sales with `t.timestamp.date() == target_date`, where `target_date` came
      from `date.today()`. A payout run while the local date is ahead of the UTC
      date would sum the wrong day and write a `Payout` row dated in the future.
    * `app/routes/payments.py:83` (and the same block in `nfc_payment` and
      `sync_offline_payments`) — the `daily_limit` window is `date.today()`
      compared against UTC timestamps, so the spending day would not line up with
      the school day.
  **Both are correct ONLY while the container runs `TZ=UTC`.** That is Railway's
  default and is now set explicitly in Railway → Variables (confirmed 2026-08-22),
  so this is latent, not live. It becomes a live bug the moment anyone sets `TZ`
  to `Africa/Kampala` (UTC+3) or deploys anywhere with a non-UTC clock — the
  payout case would silently miss a full day of a merchant's sales. Flagged only,
  not changed; fixing it properly means picking one basis and converting at the
  boundary, which touches money code.
  Confirm `SETTLEMENT_SECRET` is actually set in Railway → Variables; if it's ever
  relying on the fallback, rotate it. Flagged only, not changed.

---

- **`_is_test_mode()`'s APP_ENV fallback is a migration shim.** Added 2026-08-24
  alongside `YO_LIVE`. With `YO_LIVE` unset, `_is_test_mode()` still falls back to
  `APP_ENV != "production"` — which partly re-couples the two axes (network on/off
  vs which Yo certs to trust) that `YO_LIVE` exists to separate. It is temporary.
  Remove it once (1) `YO_LIVE` is set explicitly in every environment including
  `tests/conftest.py`, and (2) the three test files that patch `momo.APP_ENV` to
  leave test mode are rewritten to patch `YO_LIVE`. `tests/test_yo_live_switch.py`
  pins the whole truth table, fallback included, so removal is deliberate.

## How I want you to work

- Explain your plan before doing multi-step work; wait for my go on UI changes.
- Prefer small, reviewable steps over large silent ones.
- When a test fails, tell me whether it's a test bug or a real code bug — and if
  it's in money code, stop and report rather than fixing.
- Keep the same caution I do around anything touching money, keys, or the live
  system. Speed is welcome for UI and tests; care is required near the money.
