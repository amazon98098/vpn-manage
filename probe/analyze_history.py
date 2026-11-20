"""Summarize a dumped vendor chat into message templates and button patterns."""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
src = BASE / (sys.argv[1] if len(sys.argv) > 1 else "history_main_bombejokerbot.json")
msgs = json.loads(src.read_text(encoding="utf-8"))
msgs.sort(key=lambda m: m["id"])

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def template(text: str) -> str:
    t = text.translate(FA_DIGITS)
    t = re.sub(r"https?://\S+", "<URL>", t)
    t = re.sub(r"(vless|vmess|trojan|ss)://\S+", "<CONFIG>", t)
    t = re.sub(r"\d[\d,.:/\-]*", "#", t)
    t = re.sub(r"[A-Za-z0-9_]{3,}", "<W>", t)
    first = [ln.strip() for ln in t.splitlines() if ln.strip()][:3]
    return " | ".join(first)[:160]


def cb_prefix(data: str) -> str:
    return re.split(r"[_\-:]", data, maxsplit=1)[0] if data else ""


incoming = [m for m in msgs if not m["out"]]
outgoing = [m for m in msgs if m["out"]]

report = []
report.append(f"messages={len(msgs)} incoming={len(incoming)} outgoing={len(outgoing)}")
report.append(f"range={msgs[0]['date']} -> {msgs[-1]['date']}")

by_month = Counter(m["date"][:7] for m in msgs)
report.append("per_month=" + ", ".join(f"{k}:{v}" for k, v in sorted(by_month.items())))

report.append("\n=== OUTGOING TEXT (top 60) ===")
out_c = Counter(template(m["text"]) if m["text"] else f"<media:{m['media']}>" for m in outgoing)
for k, v in out_c.most_common(60):
    report.append(f"{v:5d}  {k}")

report.append("\n=== INCOMING TEMPLATES (top 120) ===")
in_c = Counter(template(m["text"]) if m["text"] else f"<media:{m['media']}>" for m in incoming)
examples = {}
for m in incoming:
    k = template(m["text"]) if m["text"] else f"<media:{m['media']}>"
    examples.setdefault(k, m)
for k, v in in_c.most_common(120):
    report.append(f"{v:5d}  {k}")

report.append("\n=== CALLBACK PREFIXES ===")
cb = Counter()
cb_examples = defaultdict(set)
for m in incoming:
    for row in m["buttons"]:
        for b in row:
            d = b.get("callback_data")
            if d:
                p = cb_prefix(d)
                cb[p] += 1
                if len(cb_examples[p]) < 4:
                    cb_examples[p].add(f"{b['text']} => {d}")
for k, v in cb.most_common(80):
    report.append(f"{v:5d}  {k}  ::  " + " || ".join(sorted(cb_examples[k])))

(BASE / "analysis_summary.txt").write_text("\n".join(report), encoding="utf-8")

ex_out = []
for k, v in in_c.most_common(120):
    m = examples[k]
    ex_out.append({"count": v, "template": k, "id": m["id"], "date": m["date"], "text": m["text"], "buttons": m["buttons"]})
(BASE / "analysis_examples.json").write_text(json.dumps(ex_out, ensure_ascii=False, indent=1), encoding="utf-8")
print("OK")
