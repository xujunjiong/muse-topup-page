#!/usr/bin/env python3
"""Local mock test for topup.py v3 (Sondev). No real money, no real API.

Mock Sondev on 127.0.0.1:18099 verifies HMAC signatures with a fake
secret, then the real topup.py runs against it. Checks:
  1. valid invite -> PROCESSING -> status poll -> DELIVERED -> done
  2. double confirm while ordered -> no second supplier call
  3. PARTIAL_DELIVERED (invite hit its referral cap) -> done, not stuck
  4. bad invite (400) -> code released to unused
  5. insufficient balance (402) -> code released to unused
  6. ordering row with no order on Sondev (404) -> released to unused
  7. zero HMAC signature failures
"""
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_PORT = 18099
TOPUP_PORT = 8090
TEST_KEY = "testkey"
TEST_SECRET = "testsecret"
DB = "/tmp/topup-test.db"
CODES = "/tmp/topup-test-codes.txt"

state = {"orders": {}, "creates": 0, "sig_fail": 0, "n": 0}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _check_sig(self, raw):
        ts = self.headers.get("X-TIMESTAMP", "")
        sig = self.headers.get("X-SIGNATURE", "")
        path = urllib.parse.urlparse(self.path).path
        to_sign = "%s\n%s\n%s\n%s" % (ts, self.command, path, raw.decode())
        want = hmac.new(TEST_SECRET.encode(), to_sign.encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            state["sig_fail"] += 1
            return False
        return True

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        import urllib.parse
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self._check_sig(raw):
            return self._send(401, {"status": "error", "error": "INVALID_SIGNATURE"})
        payload = json.loads(raw.decode() or "{}")
        ref = payload.get("ref_id")
        invite = payload.get("referral_code")
        if invite == "ZZZZZZ":
            return self._send(400, {"status": "error", "error": "INVALID_ORDER",
                                    "message": "Invalid referral_code"})
        if invite == "BAL000":
            return self._send(402, {"status": "error", "error": "INSUFFICIENT_BALANCE",
                                    "message": "Insufficient wholesale balance"})
        if ref in state["orders"]:
            o = state["orders"][ref]
            return self._send(200, {"status": "success", "data": dict(
                o, status="ALREADY_EXISTS")})
        state["n"] += 1
        o = {"status": "PROCESSING", "order_no": "ORD-TEST-%04d" % state["n"],
             "ref_id": ref, "product_code": "muse_token", "quantity": 30,
             "total_price_idr": 24000.0, "total_price_usd": 1.44,
             "payment_status": "PAID", "delivery_status": "PENDING",
             "referral_code": invite}
        state["orders"][ref] = o
        state["creates"] += 1
        return self._send(200, {"status": "success", "data": o})

    def do_GET(self):
        import urllib.parse
        if not self._check_sig(b""):
            return self._send(401, {"status": "error", "error": "INVALID_SIGNATURE"})
        ref = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
        o = state["orders"].get(ref)
        if not o:
            return self._send(404, {"status": "error", "error": "ORDER_NOT_FOUND"})
        delivery = ("PARTIAL_DELIVERED" if o.get("referral_code") == "PART01"
                    else "DELIVERED")
        done = dict(o, delivery_status=delivery)
        return self._send(200, {"status": "success", "data": done})


def http(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Host": "k123456789.com"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", name, extra)
    if not cond:
        check.failed = True


check.failed = False

for f in (DB, CODES):
    if os.path.exists(f):
        os.remove(f)
open(CODES, "w").write("aaaabbbbccccdddd\neeeeffffgggghhhh\nmmmmnnnnoooopppp\n")

mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
threading.Thread(target=mock.serve_forever, daemon=True).start()

env = dict(os.environ, SONDEV_KEY=TEST_KEY, SONDEV_SECRET=TEST_SECRET,
           SONDEV_BASE="http://127.0.0.1:%d" % MOCK_PORT,
           TOPUP_DB=DB, TOPUP_CODES=CODES)
proc = subprocess.Popen([sys.executable, os.path.expanduser("~/workspace/topup/topup.py")],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(30):
        try:
            s, _ = http("GET", "http://127.0.0.1:%d/health" % TOPUP_PORT)
            if s == 200:
                break
        except Exception:
            pass
        time.sleep(0.3)
    s, b = http("GET", "http://127.0.0.1:%d/health" % TOPUP_PORT)
    check("health 200", s == 200, b[:60])

    s, b = http("GET", "http://127.0.0.1:%d/topup/aaaabbbbccccdddd" % TOPUP_PORT)
    check("page renders unused code", s == 200 and "300亿" in b)

    s, b = http("POST", "http://127.0.0.1:%d/topup/aaaabbbbccccdddd/confirm" % TOPUP_PORT,
                {"invite_code": "ABCDEF"})
    d = json.loads(b)
    check("confirm valid invite ok", d.get("ok") is True and "下单成功" in d.get("message", ""), b[:120])
    check("supplier create called once", state["creates"] == 1, str(state["creates"]))
    c = sqlite3.connect(DB)
    row = c.execute("SELECT status,batch_id FROM codes WHERE code=?",
                    ("aaaabbbbccccdddd",)).fetchone()
    check("row ordered with order_no", row == ("ordered", "ORD-TEST-0001"), str(row))

    s, b = http("POST", "http://127.0.0.1:%d/topup/aaaabbbbccccdddd/confirm" % TOPUP_PORT,
                {"invite_code": "ABCDEF"})
    d = json.loads(b)
    check("double confirm no second call", d.get("ok") is True and state["creates"] == 1, b[:120])

    s, b = http("GET", "http://127.0.0.1:%d/topup/aaaabbbbccccdddd/status" % TOPUP_PORT)
    d = json.loads(b)
    check("status poll -> done", d.get("status") == "done", b[:160])
    row = c.execute("SELECT status FROM codes WHERE code=?",
                    ("aaaabbbbccccdddd",)).fetchone()
    check("row done in db", row == ("done",), str(row))

    s, b = http("POST", "http://127.0.0.1:%d/topup/mmmmnnnnoooopppp/confirm" % TOPUP_PORT,
                {"invite_code": "PART01"})
    d = json.loads(b)
    check("partial confirm ok", d.get("ok") is True and "下单成功" in d.get("message", ""), b[:120])
    s, b = http("GET", "http://127.0.0.1:%d/topup/mmmmnnnnoooopppp/status" % TOPUP_PORT)
    d = json.loads(b)
    check("partial delivery counts as done", d.get("status") == "done", b[:160])
    row = c.execute("SELECT status FROM codes WHERE code=?",
                    ("mmmmnnnnoooopppp",)).fetchone()
    check("partial row done in db", row == ("done",), str(row))

    s, b = http("POST", "http://127.0.0.1:%d/topup/eeeeffffgggghhhh/confirm" % TOPUP_PORT,
                {"invite_code": "ZZZZZZ"})
    d = json.loads(b)
    check("bad invite rejected", d.get("ok") is False and "邀请码无效" in d.get("message", ""), b[:120])
    row = c.execute("SELECT status FROM codes WHERE code=?",
                    ("eeeeffffgggghhhh",)).fetchone()
    check("bad invite code released unused", row == ("unused",), str(row))
    # 2 creates so far: the valid order + the partial-delivery order.
    check("no order created for bad invite", state["creates"] == 2, str(state["creates"]))

    s, b = http("POST", "http://127.0.0.1:%d/topup/eeeeffffgggghhhh/confirm" % TOPUP_PORT,
                {"invite_code": "BAL000"})
    d = json.loads(b)
    check("insufficient balance rejected", d.get("ok") is False and "余额不足" in d.get("message", ""), b[:120])
    row = c.execute("SELECT status FROM codes WHERE code=?",
                    ("eeeeffffgggghhhh",)).fetchone()
    check("balance-fail code released unused", row == ("unused",), str(row))

    c.execute("INSERT OR REPLACE INTO codes(code,status,invite_code) VALUES(?,?,?)",
              ("iiiijjjjkkkkllll", "ordering", "ABCDEF"))
    c.commit()
    s, b = http("GET", "http://127.0.0.1:%d/topup/iiiijjjjkkkkllll/status" % TOPUP_PORT)
    d = json.loads(b)
    check("ordering+404 released", d.get("status") == "unused", b[:160])
    c.close()

    check("zero signature failures", state["sig_fail"] == 0, str(state["sig_fail"]))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    mock.shutdown()
    for f in (DB, CODES):
        if os.path.exists(f):
            os.remove(f)

print("ALL PASS" if not check.failed else "SOME FAILED")
sys.exit(0 if not check.failed else 1)
