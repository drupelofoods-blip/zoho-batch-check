#!/usr/bin/env python3
"""
Daily Zoho POS batch price check for Dry Fruit Mall.

Modes (RUN_MODE env var):
  test   - quick read-only check: refreshes the token, reads 1 page of items and
           the batches of up to 3 batch-tracked items, prints field names. No writes.
  report - full read-only run: checks every item/batch and reports problems. No writes.
  fix    - full run; additionally sets a batch's selling price (sales_rate) to that
           batch's MRP (label_rate) when they differ and MRP > 0, then re-reads to
           confirm. Nothing else is ever changed.

Safety rules built in:
  - One request at a time, at least 1.1 s apart (never parallel).
  - Zoho code 43 / HTTP 429 -> wait 5 minutes, then retry.
  - Stops cleanly before MAX_REQUESTS (protects your daily API quota).
Uses only the Python standard library (no third-party packages).
"""
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

ORG_ID = os.environ.get("ZOHO_ORG_ID", "60088845017")
API_BASE = (os.environ.get("ZOHO_API_BASE") or "https://api.zakya.in/inventory/v1").rstrip("/")
ACCOUNTS = (os.environ.get("ZOHO_ACCOUNTS_URL") or "https://accounts.zoho.in").rstrip("/")
MODE = (os.environ.get("RUN_MODE") or "report").strip().lower()
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS") or "1400")
MIN_GAP = 1.1          # seconds between requests - do not lower
RATE_LIMIT_WAIT = 300  # seconds to wait on code 43
IST = timezone(timedelta(hours=5, minutes=30))

BATCH_FLAG_KEYS = ("track_batch_number", "is_batch_tracked", "track_batch", "batch_tracking")
QTY_KEYS = ("balance_quantity", "quantity_available", "available_quantity", "quantity", "stock_on_hand")


class Stop(Exception):
    """Fatal problem: stop the run and report it."""


_last_call = 0.0
_requests = 0
_token = None
_token_at = 0.0


def log(msg):
    print(msg, flush=True)


def pace():
    global _last_call
    wait = _last_call + MIN_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def refresh_token():
    global _token, _token_at
    needed = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        raise Stop("Missing GitHub secrets: " + ", ".join(missing))
    data = urllib.parse.urlencode({
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }).encode()
    pace()
    req = urllib.request.Request(f"{ACCOUNTS}/oauth/v2/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = {"error": f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}"}
    if "access_token" not in body:
        raise Stop(f"Could not get a Zoho access token: {body.get('error', body)}. "
                   "Check the client ID/secret/refresh token secrets.")
    _token, _token_at = body["access_token"], time.monotonic()
    log("Got Zoho access token.")


def api(method, path, params=None, form=None):
    """One paced, retried API call. Returns the parsed JSON body (code == 0)."""
    global _requests, _token
    params = dict(params or {})
    params["organization_id"] = ORG_ID
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    auth_retried = False
    for _attempt in range(8):
        if _token is None or time.monotonic() - _token_at > 50 * 60:
            refresh_token()
        if _requests >= MAX_REQUESTS:
            raise Stop(f"Stopped at {MAX_REQUESTS} requests to protect the daily API quota.")
        pace()
        _requests += 1
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Zoho-oauthtoken {_token}",
            "Accept": "application/json",
        })
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status = r.status
                raw = r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read().decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"Network error on {method} {path}: {e}. Retrying in 30 s.")
            time.sleep(30)
            continue
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"code": -1, "message": raw[:200]}
        code = body.get("code")
        if code == 43 or status == 429:
            log(f"Zoho rate limit (code {code}, HTTP {status}). Waiting 5 minutes.")
            time.sleep(RATE_LIMIT_WAIT)
            continue
        if status == 401 and not auth_retried:
            auth_retried = True
            _token = None
            continue
        if status < 400 and code == 0:
            return body
        raise Stop(f"{method} {path} failed: HTTP {status}, code {code}, message: {body.get('message')}")
    raise Stop(f"Gave up on {method} {path} after repeated retries.")


def first(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def batch_list(body):
    for k in ("batches", "item_batches", "batch_details"):
        if isinstance(body.get(k), list):
            return body[k]
    return []


def get_batches(item_id):
    body = api("GET", "items/batches", {"item_id": item_id, "include_empty_batches": "true"})
    return batch_list(body), body


def list_items(max_pages=None):
    items, page = [], 1
    while True:
        body = api("GET", "items", {"page": page, "per_page": 200, "filter_by": "Status.All"})
        items.extend(body.get("items", []))
        log(f"Items page {page}: total so far {len(items)}")
        more = (body.get("page_context") or {}).get("has_more_page")
        if not more or (max_pages and page >= max_pages):
            return items
        page += 1


def is_batch_item(item, flag_key):
    if flag_key is None:
        return True  # flag not exposed in list -> check every item
    v = item.get(flag_key)
    return v is True or str(v).lower() in ("true", "1", "yes")


def detect_flag(items):
    for k in BATCH_FLAG_KEYS:
        if any(k in it for it in items):
            return k
    return None


# ---------------------------------------------------------------- test mode
def run_test():
    items = list_items(max_pages=1)
    if not items:
        raise Stop("Item list came back empty - check organization ID / API base URL.")
    log("\nFields on an item: " + ", ".join(sorted(items[0].keys())))
    flag = detect_flag(items)
    log(f"Batch-tracking field: {flag or 'NOT FOUND (full run would check every item)'}")
    sample = [it for it in items if is_batch_item(it, flag)][:3]
    lines = [f"Test OK: token works, read {len(items)} items from page 1.",
             f"Batch-tracking field: {flag or 'not found'}"]
    for it in sample:
        batches, body = get_batches(it["item_id"])
        log(f"\nItem {it.get('sku')} / {it.get('name')}: {len(batches)} batches. "
            f"Response keys: {', '.join(body.keys())}")
        if batches:
            b = batches[0]
            log("  Fields on a batch: " + ", ".join(sorted(b.keys())))
            log(f"  e.g. batch {b.get('batch_number')}: sales_rate={b.get('sales_rate')} "
                f"label_rate={b.get('label_rate')} qty={first(b, QTY_KEYS)} expiry={b.get('expiry_date')}")
        lines.append(f"- {it.get('sku')} {it.get('name')}: {len(batches)} batches read")
    lines.append("No changes were made (test mode).")
    return "Zoho batch check: test OK", "\n".join(lines)


# ---------------------------------------------------------------- full run
def run_full(fix):
    today = datetime.now(IST).date()
    soon = today + timedelta(days=7)
    items = list_items()
    flag = detect_flag(items)
    if flag is None:
        log("WARNING: batch-tracking field not found on items; checking every item.")
    batch_items = [it for it in items if is_batch_item(it, flag)]
    log(f"{len(items)} items, {len(batch_items)} batch-tracked.")

    fixed, fix_failed = [], []
    mrp_mismatch, no_mrp, item_price, expired, expiring = [], [], [], [], []
    n_batches = 0

    for it in items:
        rate, mrp = num(it.get("rate")), num(first(it, ("label_rate", "mrp")))
        if mrp > 0 and rate != mrp:
            item_price.append((it.get("sku"), it.get("name"), rate, mrp))

    for idx, it in enumerate(batch_items, 1):
        sku, name, item_mrp = it.get("sku") or "", it.get("name") or "", num(first(it, ("label_rate", "mrp")))
        batches, _ = get_batches(it["item_id"])
        n_batches += len(batches)
        to_fix = []
        for b in batches:
            bno = b.get("batch_number") or b.get("batch_id")
            sr, lr = num(b.get("sales_rate")), num(b.get("label_rate"))
            qty = num(first(b, QTY_KEYS, 0))
            if lr > 0 and item_mrp > 0 and lr != item_mrp:
                mrp_mismatch.append((sku, name, bno, lr, item_mrp))
            if sr > 0 and lr <= 0:
                no_mrp.append((sku, name, bno, sr))
            exp = parse_date(b.get("expiry_date"))
            if exp and qty > 0:
                if exp < today:
                    expired.append((sku, name, bno, exp, qty))
                elif exp <= soon:
                    expiring.append((sku, name, bno, exp, qty))
            if lr > 0 and sr != lr:
                to_fix.append((b, sr, lr, qty))

        if to_fix and fix:
            for b, sr, lr, qty in to_fix:
                payload = {k: b.get(k, "") for k in (
                    "batch_id", "batch_number", "manufacturer_batch_number",
                    "manufactured_date", "expiry_date")}
                payload["sales_rate"] = b.get("label_rate")
                payload["label_rate"] = b.get("label_rate")
                payload["custom_fields"] = b.get("custom_fields") or []
                payload["batch_custom_fields"] = b.get("batch_custom_fields") or []
                try:
                    api("PUT", f"items/batches/{b['batch_id']}", form={"JSONString": json.dumps(payload)})
                except Stop as e:
                    fix_failed.append((sku, name, b.get("batch_number"), sr, lr, qty, str(e)))
            # re-read once per item to confirm
            after = {str(x.get("batch_id")): x for x in get_batches(it["item_id"])[0]}
            for b, sr, lr, qty in to_fix:
                if any(f[2] == b.get("batch_number") and f[0] == sku for f in fix_failed):
                    continue
                now = after.get(str(b.get("batch_id")), {})
                if num(now.get("sales_rate")) == lr:
                    fixed.append((sku, name, b.get("batch_number"), sr, lr, qty))
                else:
                    fix_failed.append((sku, name, b.get("batch_number"), sr, lr, qty,
                                       f"not saved (now {now.get('sales_rate')})"))
        elif to_fix:
            for b, sr, lr, qty in to_fix:
                fixed.append((sku, name, b.get("batch_number"), sr, lr, qty))  # would-fix list
        if idx % 50 == 0:
            log(f"Checked {idx}/{len(batch_items)} batch-tracked items ({_requests} requests).")

    return build_summary(fix, len(items), len(batch_items), n_batches, fixed, fix_failed,
                         mrp_mismatch, no_mrp, item_price, expired, expiring)


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_summary(fix, n_items, n_batch_items, n_batches, fixed, failed,
                  mrp_mismatch, no_mrp, item_price, expired, expiring):
    lines = [f"Checked {n_items} items ({n_batch_items} batch-tracked), {n_batches} batches. "
             f"{_requests} API requests."]
    problems = any([fixed, failed, mrp_mismatch, no_mrp, item_price, expired, expiring])
    if not problems:
        lines.append("\nEverything looks right - nothing to fix or report.")
        return "Zoho batch check: all good", "\n".join(lines)
    if fixed:
        title = "Batches fixed (selling price set to MRP)" if fix else \
                "Batches that WOULD be fixed (report mode - nothing changed)"
        lines += [f"\n### {title}", table(["SKU", "Item", "Batch", "Old → New", "Qty"],
                  [(s, n, b, f"{o} → {nw}", q) for s, n, b, o, nw, q in fixed])]
    if failed:
        lines += ["\n### Fixes that FAILED", table(["SKU", "Item", "Batch", "Old → New", "Qty", "Reason"],
                  [(s, n, b, f"{o} → {nw}", q, r) for s, n, b, o, nw, q, r in failed])]
    if mrp_mismatch:
        lines += ["\n### Batch MRP differs from item MRP (not changed)",
                  table(["SKU", "Item", "Batch", "Batch MRP", "Item MRP"], mrp_mismatch)]
    if no_mrp:
        lines += ["\n### Batches with a selling price but no MRP (not changed)",
                  table(["SKU", "Item", "Batch", "Selling price"], no_mrp)]
    if item_price:
        lines += ["\n### Item selling price differs from item MRP (not changed)",
                  table(["SKU", "Item", "Selling price", "MRP"], item_price)]
    if expired:
        lines += ["\n### Expired batches still in stock", table(["SKU", "Item", "Batch", "Expiry", "Qty"], expired)]
    if expiring:
        lines += ["\n### In-stock batches expiring within 7 days",
                  table(["SKU", "Item", "Batch", "Expiry", "Qty"], expiring)]
    subj = f"Zoho batch check: {len(fixed)} {'fixed' if fix else 'to fix'}"
    if failed:
        subj += f", {len(failed)} FAILED"
    return subj, "\n".join(lines)


# ---------------------------------------------------------------- output
def deliver(subject, text):
    log("\n" + subject + "\n" + text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## {subject}\n\n{text}\n")
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"))
    if not (user and pw and to):
        log("Email not configured (SMTP_USER / SMTP_PASSWORD / MAIL_TO) - skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    log(f"Summary emailed to {to}.")


def main():
    if MODE not in ("test", "report", "fix"):
        raise Stop(f"Unknown RUN_MODE '{MODE}' (use test, report or fix).")
    log(f"Mode: {MODE}  org: {ORG_ID}  api: {API_BASE}")
    if MODE == "test":
        subject, text = run_test()
    else:
        subject, text = run_full(fix=(MODE == "fix"))
    deliver(subject, text)


if __name__ == "__main__":
    try:
        main()
    except Stop as e:
        try:
            deliver("Zoho batch check FAILED", f"{e}\n\n{_requests} API requests made before stopping.")
        finally:
            sys.exit(1)
