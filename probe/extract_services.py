"""Build a list of known NetPlus services from history (read-only)."""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
msgs = json.loads((BASE / "history_main_bombejokerbot.json").read_text(encoding="utf-8"))
msgs.sort(key=lambda m: m["id"])

services = {}
for m in msgs:
    if m["out"]:
        continue
    for row in m["buttons"]:
        for b in row:
            d = b.get("callback_data") or ""
            if d.startswith("product_"):
                name = b["text"].strip("✨ ").strip()
                sid = d.split("_", 1)[1]
                s = services.setdefault(name.lower(), {"username": name, "service_id": sid})
                s["last_seen_list"] = m["date"]
    t = m["text"]
    mm = re.search(r"سرویس\s+(\S+)\s+از لیست سرویس های شما حذف", t)
    if mm:
        services.setdefault(mm.group(1).lower(), {"username": mm.group(1)})["deleted_at"] = m["date"]
    for pat in (r"نام کاربری سرویس\s*:\s*(\S+)", r"نام سرویس\s*:\s*(\S+)"):
        mm = re.search(pat, t)
        if mm and ("ایجاد شد" in t or "تمدید" in t):
            s = services.setdefault(mm.group(1).lower(), {"username": mm.group(1)})
            key = "created_at" if "ایجاد شد" in t else "last_renewed_at"
            s[key] = m["date"]
            s.pop("deleted_at", None) if key == "created_at" else None

active = {k: v for k, v in services.items() if "deleted_at" not in v}
deleted = {k: v for k, v in services.items() if "deleted_at" in v}
prefixed = sum(1 for v in active.values() if re.match(r"^\d+_", v["username"]))
out = {"total_seen": len(services), "active_estimate": len(active), "deleted": len(deleted),
       "with_number_prefix": prefixed, "services": sorted(services.values(), key=lambda v: v["username"].lower())}
(BASE / "netplus_services_from_history.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"seen={len(services)} active_est={len(active)} deleted={len(deleted)} numbered={prefixed}")
