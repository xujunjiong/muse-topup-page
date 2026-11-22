#!/usr/bin/env python3
"""Top-up redemption page for k123456789.com/topup/<code> — v3 with Sondev API.

Flow (user-confirmed 2026-11-19, supplier switched to Sondev 2026-11-21):
  * Buyer buys a code in the shop; delivery is a link
    https://k123456789.com/topup/<xxx> (xxx = pre-generated 16-char code).
  * Page shows the top-up code (fixed 30B), asks for the buyer's OWN
    Muse invite code (exactly 6 alphanumeric), Confirm.
  * Confirm calls Sondev POST /api/v1/orders/create directly:
    {"ref_id": "topup-<xxx>", "product_code": "muse_token",
     "quantity": 30, "referral_code": "<INVITE>"}.
    HMAC-SHA256 signed (X-API-KEY / X-TIMESTAMP / X-SIGNATURE).
    Live reseller price 2026-11-21: Rp 800 / $0.048 per 1B,
    30B = Rp 24,000 (~$1.44), deducted from the Sondev IDR balance.

Double-charge prevention — the whole point of this version:
  * Atomic claim: the code is moved unused -> ordering in a single
    BEGIN IMMEDIATE transaction BEFORE any supplier call, with the
    invite code recorded. A second confirm for the same code (double
    click, refresh, retry) sees ordering/ordered and returns the
    existing order instead of ordering again.
  * ref_id is deterministic per code ("topup-<xxx>"), so Sondev itself
    also refuses a second charge: a repeat create returns
    ALREADY_EXISTS with the original order, never a new deduction.
  * PROCESSING -> status ordered, frontend polls status.
  * FAILED -> status failed, locked for manual review. Sondev refunds
    a failed order's balance automatically; any buyer-side refund
    (if the code was already sold) is manual.
  * Definitive supplier failure with no order created (400 invalid
    invite, 402 insufficient balance, 409 sold out, 401/403 auth) ->
    code reverts to unused, so the buyer can retry. Per Sondev docs
    these failures do not charge.
  * Network timeout / unknown outcome -> needs_check, locked for
    manual review, never auto-re-ordered. Resolve by polling
    GET /api/v1/orders/topup-<xxx>: if Sondev has the order, its
    delivery_status decides. If it 404s while the code is still
    "ordering" with no order number, the create never landed and the
    status check releases the code back to unused.
  * Supplier status failed -> status failed, locked for manual review
    (do not auto-revert: a charged-then-failed order must be checked
    before the code is reusable).

Sondev key + secret: read from /opt/topup/.env (600, root-only) if
present, else SONDEV_KEY / SONDEV_SECRET env. Never logged, never
sent to the browser, never stored in this repo.
Stdlib only. Runs on 127.0.0.1:8090 behind nginx (k123456789.com).
"""
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("TOPUP_DB", "/opt/topup/topup.db")
CODES_PATH = os.environ.get("TOPUP_CODES", "/opt/topup/codes.txt")
HOST = "127.0.0.1"
PORT = 8090
SUPPLIER_BASE = os.environ.get("SONDEV_BASE", "https://sondevs.tech")
QTY_BILLIONS = 30
TIMEOUT = 30

CODE_RE = re.compile(r"^[a-z0-9]{16}$")
INVITE_RE = re.compile(r"^[A-Za-z0-9]{6}$")


def _load_keys():
    for path in ("/opt/topup/.env",):
        try:
            with open(path, encoding="utf-8") as fh:
                vals = {}
                for line in fh:
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        vals[k.strip()] = v.strip().strip("\"'")
                if vals.get("SONDEV_KEY") and vals.get("SONDEV_SECRET"):
                    return vals["SONDEV_KEY"], vals["SONDEV_SECRET"]
        except OSError:
            pass
    k = os.environ.get("SONDEV_KEY") or ""
    s = os.environ.get("SONDEV_SECRET") or ""
    if k and s:
        return k, s
    raise SystemExit("SONDEV_KEY/SONDEV_SECRET not found in /opt/topup/.env or environment")


API_KEY, API_SECRET = _load_keys()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _sondev(method, path, payload=None):
    """Returns (http_status, data_dict). Raises RuntimeError on network failure.

    Every request is HMAC-SHA256 signed per Sondev docs:
    signature = hex(HMAC_SHA256(API_SECRET, TIMESTAMP + "\\n" + METHOD
    + "\\n" + PATH + "\\n" + BODY)), BODY compact JSON, byte-for-byte
    identical to the body sent. GET signs an empty body (trailing "\\n").
    """
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    ts = str(int(time.time()))
    to_sign = "%s\n%s\n%s\n%s" % (ts, method, path, body)
    sig = hmac.new(API_SECRET.encode(), to_sign.encode(),
                   hashlib.sha256).hexdigest()
    url = SUPPLIER_BASE + path
    data = body.encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "muse-topup/3.0",
                 "X-API-KEY": API_KEY,
                 "X-TIMESTAMP": ts,
                 "X-SIGNATURE": sig},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("supplier request failed: %s" % exc)


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS codes("
        "code TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'unused', "
        "used_at TEXT, invite_code TEXT, batch_id TEXT, "
        "supplier_status TEXT, last_error TEXT, updated_at TEXT)"
    )
    # Migrate v1 DB (only code/status/used_at) by adding missing columns.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(codes)")}
    for col, ddl in (
        ("invite_code", "TEXT"), ("batch_id", "TEXT"),
        ("supplier_status", "TEXT"), ("last_error", "TEXT"),
        ("updated_at", "TEXT"),
    ):
        if col not in existing:
            conn.execute("ALTER TABLE codes ADD COLUMN %s %s" % (col, ddl))
    conn.commit()
    return conn


def _get_row(code):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT status, invite_code, batch_id, supplier_status "
            "FROM codes WHERE code=?", (code,)
        ).fetchone()
        if not row:
            return None
        return {"status": row[0], "invite_code": row[1], "batch_id": row[2],
                "supplier_status": row[3]}
    finally:
        conn.close()


def _set(code, **kw):
    conn = _db()
    try:
        sets = ", ".join("%s=?" % k for k in kw)
        conn.execute("UPDATE codes SET %s, updated_at=? WHERE code=?"
                     % sets, (*kw.values(), _now(), code))
        conn.commit()
    finally:
        conn.close()


def _claim(code, invite):
    """Atomically move unused -> ordering. Returns (ok, existing_row)."""
    conn = _db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, invite_code, batch_id FROM codes WHERE code=?",
            (code,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return False, None
        if row[0] != "unused":
            conn.execute("ROLLBACK")
            return False, {"status": row[0], "invite_code": row[1], "batch_id": row[2]}
        conn.execute(
            "UPDATE codes SET status='ordering', invite_code=?, "
            "batch_id=NULL, supplier_status=NULL, last_error=NULL, "
            "updated_at=? WHERE code=?", (invite, _now(), code))
        conn.execute("COMMIT")
        return True, None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _poll_supplier(ref_id):
    """Poll Sondev order status by ref_id.

    Returns (supplier_status, is_done) where supplier_status is one of
    "done" / "failed" / "pending" / "not_found" / None.
    "not_found" (HTTP 404 ORDER_NOT_FOUND) means no order exists under
    this ref_id; None means the poll itself failed (network/5xx) and
    the outcome is still unknown.
    """
    status, data = _sondev("GET", "/api/v1/orders/" + ref_id)
    if status == 404:
        return "not_found", False
    if status != 200 or not isinstance(data, dict):
        return None, False
    data = data.get("data") or {}
    delivery = data.get("delivery_status")
    # Owner direction 2026-11-22: PARTIAL_DELIVERED counts as done.
    # Whatever Sondev delivered (e.g. invite hit its referral cap) is
    # final; the page shows completed, no buyer top-up of the shortfall.
    if delivery in ("DELIVERED", "PARTIAL_DELIVERED"):
        return "done", True
    if delivery == "FAILED":
        return "failed", True
    if delivery in ("PENDING", "PROCESSING"):
        return "pending", False
    return None, False


PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#222;margin:0;padding:24px 16px;min-height:100vh;min-height:100svh;display:flex;align-items:center;justify-content:center;box-sizing:border-box}
.card{width:100%;max-width:480px;margin:16px auto;background:#fff;border-radius:14px;padding:32px 24px;box-sizing:border-box;box-shadow:0 2px 16px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 8px}.hero{text-align:center;margin:6px 0 18px}.hero b{display:block;font-size:56px;line-height:1.1;font-weight:800;color:#2f6bff;letter-spacing:2px}.hero span{display:block;margin-top:8px;font-size:13px;color:#888}.sub{color:#666;font-size:14px;line-height:1.6;margin:0 0 20px}
.status{font-size:13px;margin-top:6px}.ok{color:#0a7d2c}.bad{color:#c00}.warn{color:#b45309}
label{display:block;font-size:14px;font-weight:600;margin:0 0 6px}
input{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border:1px solid #ccc;border-radius:8px;text-transform:uppercase}
input:focus{outline:none;border-color:#2f6bff}
button{width:100%;margin-top:16px;font-size:16px;font-weight:700;color:#fff;background:#2f6bff;border:0;border-radius:8px;padding:15px;cursor:pointer}
button:disabled{background:#9db4f5;cursor:not-allowed}
.msg{margin-top:14px;font-size:14px;line-height:1.6;white-space:pre-wrap}
.msg.err{color:#c00}.msg.good{color:#0a7d2c}.msg.warn{color:#b45309}
.note{margin-top:18px;font-size:12px;color:#888;line-height:1.6}
.boxes{display:flex;gap:10px;margin:0 0 6px}
.boxes input{flex:1 1 0;min-width:0;width:auto;height:64px;text-align:center;font-size:28px;font-weight:700;padding:0;text-transform:uppercase}
.boxes input:disabled{background:#f6f7f9;color:#222}
"""

POLL_JS = """
async function pollStatus() {
  try {
    const r = await fetch(location.pathname + '/status');
    const d = await r.json();
    const msg = document.getElementById('msg');
    if (!msg) return;
    if (d.supplier_status === 'done' || d.status === 'done') {
      msg.textContent = '充值完成，一般 1-2 分钟到账。';
      msg.className = 'msg good'; if (timer) clearInterval(timer); return;
    }
    if (d.status === 'failed') {
      msg.textContent = '充值失败，请联系客服处理，不要重复提交。'; msg.className = 'msg err'; if (timer) clearInterval(timer); return;
    }
    if (d.status === 'needs_check') {
      msg.textContent = '订单已扣费但结果未确认，正在回查，请勿重复提交。'; msg.className = 'msg warn'; return;
    }
    msg.textContent = '下单成功，正在充值处理中，请稍等…'; msg.className = 'msg warn';
  } catch (e) {}
}
"""


def _invite_boxes(code="", editable=False):
    """Six one-char boxes for the invite code; filled + disabled once submitted/done."""
    chars = (code or "")[:6].ljust(6)
    out = ['<div class="boxes" id="boxes">']
    for i, ch in enumerate(chars):
        dis = "" if editable else " disabled"
        out.append('<input inputmode="text" autocomplete="off" autocapitalize="characters" spellcheck="false" enterkeyhint="%s" aria-label="邀请码第 %d 位" value="%s"%s>' % ("go" if i == 5 else "next", i + 1, html.escape(ch.strip()), dis))
    out.append("</div>")
    return "".join(out)


def page(code, status, message="", msg_class="", invite_code=None):
    esc_code = html.escape(code)
    if status is None:
        status_html = '<div class="status bad">这个充值码不存在，请检查链接是否完整。</div>'
        form = ""
    elif status == "done":
        if invite_code:
            status_html = '<div class="status ok">这个充值码已经用过了，充值已完成。</div>'
        else:
            status_html = '<div class="status ok">这个充值码已经用过了，充值已完成。</div>'
        form = _invite_boxes(invite_code or "")
    elif status in ("ordering", "ordered"):
        if invite_code:
            status_html = '<div class="status warn">正在为邀请码 ' + html.escape(invite_code) + ' 充值处理中，请勿重复提交，页面会自动查询进度。</div>'
        else:
            status_html = '<div class="status warn">这个充值码正在充值处理中，请勿重复提交，页面会自动查询进度。</div>'
        form = (_invite_boxes(invite_code or "") +
                '<div id="msg" class="msg warn"></div><script>let timer = null;\n' + POLL_JS +
                '\npollStatus();timer = setInterval(pollStatus, 4000);</script>')
    elif status in ("needs_check", "failed"):
        status_html = '<div class="status bad">这个充值码的订单需要人工确认，请不要重复提交，联系客服处理。</div>'
        form = ""
    else:
        status_html = ""
        form = ("""
<form id="f">
<label>你的 Muse 邀请码（6 位字母数字）</label>
""" + _invite_boxes("", editable=True) + """
<button id="btn" type="submit">确定</button>
</form>
<div id="msg" class="msg"></div>
<script>
let timer = null;
""" + POLL_JS + """
const boxes = [...document.querySelectorAll('#boxes input')];
let composing = false;
boxes.forEach((b, i) => {
  b.addEventListener('focus', () => { if (b.value) b.select(); });
  b.addEventListener('compositionstart', () => { composing = true; });
  b.addEventListener('compositionend', () => {
    composing = false;
    b.value = b.value.replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(-1);
    if (b.value && i < 5) boxes[i + 1].focus();
  });
  b.addEventListener('beforeinput', (e) => {
    if (e.inputType === 'insertText' && b.value) b.value = '';
  });
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace') {
      b._bsKey = true;
      if (!b.value && i > 0) { e.preventDefault(); boxes[i - 1].value = ''; boxes[i - 1].focus(); }
    }
    else if (e.key === 'ArrowLeft' && i > 0) { boxes[i - 1].focus(); }
    else if (e.key === 'ArrowRight' && i < 5) { boxes[i + 1].focus(); }
  });
  b.addEventListener('input', (e) => {
    if (composing) return;
    const bsKey = b._bsKey; b._bsKey = false;
    if (e.inputType === 'deleteContentBackward' && !bsKey && !b.value && i > 0) {
      boxes[i - 1].focus(); boxes[i - 1].value = '';
      return;
    }
    const clean = b.value.replace(/[^A-Za-z0-9]/g, '').toUpperCase();
    if (clean.length === 2) {
      b.value = clean.slice(-1);
      if (i < 5) boxes[i + 1].focus();
      return;
    }
    if (clean.length > 2) {
      clean.split('').forEach((ch, j) => { if (i + j < 6) boxes[i + j].value = ch; });
      boxes[Math.min(i + clean.length - 1, 5)].focus();
      return;
    }
    b.value = clean;
    if (b.value && i < 5) boxes[i + 1].focus();
  });
  b.addEventListener('paste', (e) => {
    e.preventDefault();
    const t = (e.clipboardData.getData('text') || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 6);
    t.split('').forEach((ch, j) => { if (i + j < 6) boxes[i + j].value = ch; });
    boxes[Math.min(i + t.length, 5)].focus();
  });
});
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  const invite = boxes.map(b => b.value).join('');
  if (!/^[A-Za-z0-9]{6}$/.test(invite)) { msg.textContent = '邀请码是 6 位字母数字，请检查后重新输入。'; msg.className = 'msg err'; return; }
  btn.disabled = true; btn.textContent = '下单中…';
  try {
    const r = await fetch(location.pathname + '/confirm', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({invite_code: invite})});
    const d = await r.json();
    msg.textContent = d.message || '';
    msg.className = 'msg ' + (d.ok ? 'warn' : 'err');
    if (d.ok) { btn.textContent = '处理中…'; boxes.forEach(b => b.disabled = true); timer = setInterval(pollStatus, 4000); pollStatus(); }
    else { btn.disabled = false; btn.textContent = '确定'; }
  } catch (err) {
    msg.textContent = '网络有问题，请稍后再试，不要重复提交。'; msg.className = 'msg err';
    btn.disabled = false; btn.textContent = '确定'; }
});
</script>""")
    msg_html = f'<div class="msg {html.escape(msg_class)}">{html.escape(message)}</div>' if message else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muse 充值</title><style>{PAGE_CSS}</style></head><body>
<div class="card"><h1>Muse 充值</h1>
<div class="hero"><b>300亿</b><span>本次固定充值额度</span></div>
<p class="sub">输入你的 Muse 邀请码，确认后为该邀请码充值。</p>
{status_html}
{form}{msg_html}
<p class="note">一个充值码只能用一次，确认后请不要重复提交。下单后页面会自动查询进度，一般 1-2 分钟到账。</p>
</div></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host_ok(self):
        # Only k123456789.com may reach this service: no direct-IP and
        # no other-domain access. /health is exempt (localhost monitoring).
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host == "k123456789.com"

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/health" and not self._host_ok():
            return self._send(404, "Not Found", "text/plain")
        if path == "/health":
            return self._json({"status": "ok"})
        if path in ("/", ""):
            return self._send(200, page("", None, "请用店里发给你的充值链接打开，格式是 /topup/加充值码。"))
        m = re.match(r"^/topup/([A-Za-z0-9\-_]+)/status/?$", path)
        if m:
            return self._handle_status(m.group(1).lower())
        m = re.match(r"^/topup/([A-Za-z0-9\-_]+)/?$", path)
        if not m:
            return self._send(404, page("", None, "页面不存在。"))
        code = m.group(1).lower()
        if not CODE_RE.match(code):
            return self._send(200, page(code, None))
        row = _get_row(code)
        return self._send(200, page(code, row["status"] if row else None, invite_code=row["invite_code"] if row else None))

    def _handle_status(self, code):
        row = _get_row(code)
        if not row:
            return self._json({"ok": False, "status": None, "message": "这个充值码不存在。"})
        # Poll Sondev by ref_id when we are not terminal.
        if row["status"] in ("ordered", "needs_check", "ordering"):
            try:
                sup, done = _poll_supplier("topup-" + code)
            except Exception:
                sup, done = None, False
            if sup == "done":
                _set(code, status="done", supplier_status="done", used_at=_now())
                row["status"] = "done"; row["supplier_status"] = "done"
            elif sup == "failed":
                _set(code, status="failed", supplier_status="failed")
                row["status"] = "failed"; row["supplier_status"] = "failed"
            elif sup == "pending":
                _set(code, supplier_status="pending")
                row["supplier_status"] = "pending"
            elif sup == "not_found" and row["status"] == "ordering" and not row.get("batch_id"):
                # No order exists under this ref_id: the create never
                # landed, so the code can safely be used again.
                _set(code, status="unused", invite_code=None, batch_id=None,
                     supplier_status=None, last_error="sondev: no such order")
                row["status"] = "unused"; row["supplier_status"] = None
        return self._json({"ok": True, "status": row["status"],
                           "batch_id": row.get("batch_id"),
                           "invite_code": row.get("invite_code"),
                           "supplier_status": row.get("supplier_status")})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._host_ok():
            return self._json({"ok": False, "message": "接口不存在。"}, 404)
        m = re.match(r"^/topup/([A-Za-z0-9\-_]+)/confirm/?$", path)
        if not m:
            return self._json({"ok": False, "message": "接口不存在。"}, 404)
        code = m.group(1).lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "message": "请求格式不对。"})
        invite = str(data.get("invite_code") or "").strip().upper()
        if not CODE_RE.match(code):
            return self._json({"ok": False, "message": "这个充值码不存在。"})
        if not INVITE_RE.match(invite):
            return self._json({"ok": False, "message": "邀请码是 6 位字母数字，请检查后重新输入。"})
        # Atomic claim first — this is the double-charge guard.
        try:
            claimed, existing = _claim(code, invite)
        except Exception:
            return self._json({"ok": False, "message": "系统繁忙，请稍后再试，不要重复提交。"})
        if not claimed:
            if existing is None:
                return self._json({"ok": False, "message": "这个充值码不存在。"})
            if existing["status"] == "done":
                return self._json({"ok": False, "message": "这个充值码已经用过了，充值已完成。"})
            if existing["status"] in ("ordering", "ordered", "needs_check"):
                return self._json({"ok": True, "already": True,
                                   "batch_id": existing.get("batch_id"),
                                   "message": "这个充值码正在处理中，请勿重复提交，页面会自动查询进度。"})
            return self._json({"ok": False, "message": "这个充值码已经用过了，不能重复使用。"})
        # Claimed: now call the supplier exactly once for this code.
        try:
            http_status, resp = _sondev(
                "POST", "/api/v1/orders/create",
                {"ref_id": "topup-" + code, "product_code": "muse_token",
                 "quantity": QTY_BILLIONS, "referral_code": invite})
        except Exception as exc:
            # Unknown outcome: keep locked for manual review, never reorder.
            _set(code, status="needs_check", last_error=str(exc)[:200])
            return self._json({"ok": True, "already": False, "needs_check": True,
                               "message": "下单请求结果未知（网络问题），请不要重复提交，正在回查，稍后刷新页面查看进度。"})
        data = resp.get("data") if isinstance(resp, dict) else None
        order_no = data.get("order_no") if isinstance(data, dict) else None
        sup_status = data.get("status") if isinstance(data, dict) else None
        if http_status == 200 and order_no and sup_status in ("PROCESSING", "ALREADY_EXISTS"):
            _set(code, status="ordered", batch_id=order_no, supplier_status="pending")
            return self._json({"ok": True, "batch_id": order_no,
                               "message": "下单成功，正在为你的邀请码充值 300亿，请稍等，页面会自动查询进度。"})
        if http_status == 200 and order_no and sup_status in ("DELIVERED", "PARTIAL_DELIVERED"):
            _set(code, status="done", batch_id=order_no, supplier_status="done", used_at=_now())
            return self._json({"ok": True, "batch_id": order_no,
                               "message": "充值完成，一般 1-2 分钟到账。"})
        if http_status == 200 and sup_status == "FAILED":
            _set(code, status="failed", batch_id=order_no, supplier_status="failed",
                 last_error="sondev order failed (auto-refunded)")
            return self._json({"ok": True, "needs_check": True,
                               "message": "充值失败（供货商已自动退款），请不要重复提交，联系客服处理。"})
        # Definitive failure, no order created: per Sondev docs this does
        # not charge, so release the code for a retry.
        err = ""
        if isinstance(resp, dict):
            err = str(resp.get("error") or resp.get("message") or "")[:200]
        _set(code, status="unused", invite_code=None, batch_id=None,
             supplier_status=None, last_error=err)
        if http_status == 400:
            msg = "下单失败：邀请码无效，请检查邀请码。" + ("（%s）" % err if err else "")
        elif http_status == 402:
            msg = "供货商余额不足，请稍后再试，充值码没有被消耗。"
        elif http_status == 409:
            msg = "供货商暂时缺货，请稍后再试，充值码没有被消耗。"
        elif http_status in (401, 403):
            msg = "供货商接口未授权，请联系客服，充值码没有被消耗。"
        elif http_status == 503:
            msg = "供货商暂时无法处理，请稍后再试，充值码没有被消耗。"
        else:
            msg = "下单失败，请稍后再试，充值码没有被消耗。" + ("（%s）" % err if err else "")
        return self._json({"ok": False, "message": msg})

if __name__ == "__main__":
    import os
    os.makedirs("/opt/topup", exist_ok=True)
    conn = _db()
    if conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0:
        codes = [l.strip().lower() for l in open(CODES_PATH, encoding="utf-8") if l.strip()]
        conn.executemany("INSERT OR IGNORE INTO codes(code, status) VALUES(?, 'unused')", [(c,) for c in codes])
        conn.commit()
        print(f"imported {len(codes)} codes")
    conn.close()
    print(f"topup service listening on {HOST}:{PORT} (Sondev muse_token order, fixed {QTY_BILLIONS}B)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
