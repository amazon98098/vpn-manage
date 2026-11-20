"""Extract payment sessions and timing from NetPlus history."""
import json
import re
from datetime import datetime
from pathlib import Path
from statistics import median

BASE = Path(__file__).resolve().parent
msgs = json.loads((BASE / "history_main_bombejokerbot.json").read_text(encoding="utf-8"))
msgs.sort(key=lambda m: m["id"])
for m in msgs:
    m["t"] = datetime.fromisoformat(m["date"])

FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def has_btn(m, prefix):
    return any(
        (b.get("callback_data") or "").startswith(prefix)
        for row in m["buttons"]
        for b in row
    )


def is_pay_instr(m):
    t = m["text"]
    return (not m["out"]) and ("ریال" in t) and ("کارت" in t) and (
        "مبلغ" in t
    ) and (has_btn(m, "sendresidcart") or "شماره کارت" in t or "دقیق" in t)


def kind(m):
    t = m["text"]
    if m["out"]:
        if m["media"] == "MessageMediaPhoto":
            return "OUT_PHOTO"
        return "OUT:" + t[:40].replace("\n", " ")
    if is_pay_instr(m):
        return "PAY_INSTR"
    if "تمدید برای سرویس شما با موفقیت" in t:
        return "RENEW_OK"
    if "سرویس با موفقیت ایجاد شد" in t:
        return "CREATE_OK"
    if "تراکنش شما توسط ربات تایید" in t:
        return "TX_BOT_OK"
    if "به کیف پول شما واریز" in t or "به موجودی کیف پول" in t:
        return "WALLET_CREDIT"
    if "تصویر رسید خود را ارسال" in t:
        return "ASK_RECEIPT"
    if "رسید ثبت شد" in t:
        return "RECEIPT_SAVED"
    if "فاکتور تمدید" in t:
        return "RENEW_INVOICE"
    if "در حال ساختن سرویس" in t:
        return "BUILDING"
    if "روز باقی مانده" in t:
        return "ALERT_DAYS"
    if "مگابایت باقی مانده" in t:
        return "ALERT_MB"
    if "حذف گردید" in t:
        return "ALERT_DELETED"
    if "پایان زمان سرویس" in t:
        return "STATUS_EXPIRED"
    if "وضعیت سرویس" in t:
        return "STATUS"
    return "IN:" + t[:40].replace("\n", " ")


for m in msgs:
    m["k"] = kind(m)

lines = []

# Timing per payment session: from PAY_INSTR until next PAY_INSTR or 30 minutes
sessions = []
idx = [i for i, m in enumerate(msgs) if m["k"] == "PAY_INSTR"]
for n, i in enumerate(idx):
    start = msgs[i]
    end_i = idx[n + 1] if n + 1 < len(idx) else len(msgs)
    s = {"id": start["id"], "date": start["date"], "events": []}
    for m in msgs[i + 1 : end_i]:
        dt = (m["t"] - start["t"]).total_seconds()
        if dt > 3600:
            break
        s["events"].append((round(dt), m["k"]))
    first_ok = next((dt for dt, k in s["events"] if k in ("RENEW_OK", "CREATE_OK", "TX_BOT_OK", "WALLET_CREDIT")), None)
    receipt = next((dt for dt, k in s["events"] if k in ("ASK_RECEIPT",)), None)
    photo = next((dt for dt, k in s["events"] if k == "OUT_PHOTO"), None)
    s["first_ok"] = first_ok
    s["receipt"] = receipt
    s["photo"] = photo
    s["route"] = "receipt" if (receipt is not None and (first_ok is None or receipt < first_ok)) else ("auto" if first_ok is not None else "none")
    sessions.append(s)

auto = [s["first_ok"] for s in sessions if s["route"] == "auto"]
rec = [s for s in sessions if s["route"] == "receipt"]
none = [s for s in sessions if s["route"] == "none"]
lines.append(f"payment_sessions={len(sessions)} auto={len(auto)} receipt={len(rec)} no_outcome={len(none)}")
if auto:
    a = sorted(auto)
    lines.append(
        f"auto_ok_seconds: min={a[0]} median={median(a)} p90={a[int(len(a)*0.9)-1]} max={a[-1]}"
    )
rt = sorted(s["receipt"] for s in rec)
if rt:
    lines.append(f"receipt_prompt_seconds_after_instr: min={rt[0]} median={median(rt)} max={rt[-1]}")
okafter = sorted(s["first_ok"] for s in rec if s["first_ok"] is not None)
if okafter:
    lines.append(f"receipt_route_success_seconds: min={okafter[0]} median={median(okafter)} max={okafter[-1]}")

lines.append("\n=== SAMPLE SESSIONS (event timelines) ===")
for s in sessions[:6] + rec[:6] + none[:4]:
    lines.append(f"[{s['route']}] instr_id={s['id']} {s['date']}")
    lines.append("   " + " ; ".join(f"+{dt}s {k}" for dt, k in s["events"][:14]))

# Show the full flow before a few payment instructions (what led to it)
lines.append("\n=== WHAT LEADS TO PAYMENT (8 messages before) ===")
for i in idx[-3:] + idx[:2]:
    lines.append(f"--- before instr {msgs[i]['id']} ---")
    for m in msgs[max(0, i - 8) : i + 1]:
        lines.append(f"   {m['date'][11:19]} {m['k']}")

(BASE / "analysis_flows.txt").write_text("\n".join(lines), encoding="utf-8")

# Full text of key message kinds (latest example of each)
keys = ["PAY_INSTR", "RENEW_INVOICE", "RENEW_OK", "CREATE_OK", "TX_BOT_OK", "WALLET_CREDIT",
        "ASK_RECEIPT", "RECEIPT_SAVED", "ALERT_DAYS", "ALERT_MB", "ALERT_DELETED", "STATUS", "STATUS_EXPIRED", "BUILDING"]
ex = {}
for m in msgs:
    if m["k"] in keys:
        ex[m["k"]] = {"id": m["id"], "date": m["date"], "text": m["text"], "buttons": m["buttons"]}
# Oldest PAY_INSTR too (old format)
old_pay = next(m for m in msgs if m["k"] == "PAY_INSTR")
ex["PAY_INSTR_OLDEST"] = {"id": old_pay["id"], "date": old_pay["date"], "text": old_pay["text"], "buttons": old_pay["buttons"]}
(BASE / "analysis_key_examples.json").write_text(json.dumps(ex, ensure_ascii=False, indent=1), encoding="utf-8")
print("OK", len(sessions))
