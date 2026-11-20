"""Print recent NetPlus flows around payment instructions, with kinds and timings."""
import json
from datetime import datetime
from pathlib import Path

import analyze_flows as af  # reuses kinds

BASE = Path(__file__).resolve().parent
msgs = af.msgs
idx = [i for i, m in enumerate(msgs) if m["k"] == "PAY_INSTR"]

lines = []
for i in idx[-18:]:
    start = msgs[i]
    lines.append(f"\n##### PAY_INSTR {start['id']} {start['date']}")
    # context before
    for m in msgs[max(0, i - 7) : i]:
        dt = round((m["t"] - start["t"]).total_seconds())
        lines.append(f"   {dt:+6d}s {m['k']}")
    lines.append("   ------ instr ------")
    for m in msgs[i + 1 : i + 12]:
        dt = round((m["t"] - start["t"]).total_seconds())
        if dt > 1800:
            break
        extra = ""
        if m["k"] in ("RENEW_OK", "CREATE_OK", "WALLET_CREDIT"):
            extra = " :: " + m["text"].splitlines()[2 if m["k"] != "WALLET_CREDIT" else 0][:80]
        lines.append(f"   {dt:+6d}s {m['k']}{extra}")

# Outcome classification over all sessions with longer horizon
outcomes = {"renew": 0, "create": 0, "wallet_only": 0, "receipt": 0, "none": 0}
gaps = []
for n, i in enumerate(idx):
    end_i = idx[n + 1] if n + 1 < len(idx) else len(msgs)
    start = msgs[i]
    got = None
    for m in msgs[i + 1 : end_i]:
        dt = (m["t"] - start["t"]).total_seconds()
        if dt > 7200:
            break
        if m["k"] == "ASK_RECEIPT" and got is None:
            got = "receipt"
        if m["k"] in ("RENEW_OK", "CREATE_OK", "WALLET_CREDIT"):
            if got is None:
                got = {"RENEW_OK": "renew", "CREATE_OK": "create", "WALLET_CREDIT": "wallet_only"}[m["k"]]
                gaps.append((got, round(dt)))
            break
    outcomes[got or "none"] += 1
lines.insert(0, "outcomes=" + json.dumps(outcomes))
for kind in ("renew", "create", "wallet_only"):
    g = sorted(d for k, d in gaps if k == kind)
    if g:
        lines.insert(1, f"{kind}: n={len(g)} min={g[0]}s median={g[len(g)//2]}s p90={g[int(len(g)*0.9)-1]}s max={g[-1]}s")

(BASE / "analysis_recent.txt").write_text("\n".join(lines), encoding="utf-8")
print("OK")
