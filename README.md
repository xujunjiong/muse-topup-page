# Muse token top-up page

Redemption page for topping up existing Muse accounts: a buyer opens a
one-time link `https://k123456789.com/topup/<code>`, enters their own
6-character Muse invite code, and the page tops that code up with a fixed
30B tokens via the Sondev reseller API.

## One file: frontend + backend together

There is no separate frontend. `topup.py` is a single Python-stdlib file
that contains both:

- **Frontend**: the `page()` function returns the full HTML page (inline
  CSS + JS). The form, the invite-code input, and the status polling all
  live there.
- **Backend**: `Handler` serves the page, keeps the one-time codes in
  SQLite, and calls Sondev (`POST /api/v1/orders/create`, then polls
  `GET /api/v1/orders/{ref_id}`).

No build step, no dependencies beyond the Python 3 standard library.

## How it works

- Each code is pre-generated (16 lowercase chars) and can be used once.
- Confirm atomically claims the code (`unused` → `ordering`) before any
  supplier call; `ref_id` is `topup-<code>`, so Sondev also refuses a
  second charge for the same code (`ALREADY_EXISTS`).
- `muse_token`, quantity 30, `referral_code` = the buyer's invite code.
  Live reseller price 2026-11-21: Rp 800 / $0.048 per 1B,
  30B = Rp 24,000 (~$1.44).
- Definitive failures with no order created (400 invalid invite,
  402 insufficient balance, 409 sold out, 401/403 auth) release the code
  back to `unused`. Unknown outcomes lock the code as `needs_check` for
  manual review; failed Sondev orders are auto-refunded on their side.

## Deploy

On the server (`/opt/topup/`):

- `topup.py` — the service; systemd `topup.service` runs it on
  `127.0.0.1:8090`, nginx vhost `k123456789.com` proxies to it.
- `codes.txt` — the pre-generated codes, imported into SQLite on first start.
- `topup.db` — SQLite, `codes` table (code / status / invite_code /
  batch_id / supplier_status / last_error).
- `.env` (mode 600, root-only) — **never commit this file**:
  ```
  SONDEV_KEY=snd_live_...
  SONDEV_SECRET=sec_...
  ```
  The service also reads `SONDEV_KEY` / `SONDEV_SECRET` from the
  environment.

## Test

`test_sondev_mock.py` runs the real `topup.py` against a local mock
Sondev server (fake key/secret, no real money): confirm → processing →
delivered, double-submit guard, 400/402 release, ordering+404 release,
HMAC verification. Run: `python3 test_sondev_mock.py`.

## History

- 2026-11-19: built with the Buff Token API (fixed 30B, no quote step).
- 2026-11-21: supplier switched to Sondev; old Buff version kept on the
  server as `topup.py.bak-20261121-buff`.
- 2026-11-22: `PARTIAL_DELIVERED` counts as done (invite hit its referral
  cap, e.g. 25/30B delivered); the done page no longer claims a fixed
  300B amount — the invite code is already shown in the boxes below.
