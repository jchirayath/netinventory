"""Battery tracking: a log of battery changes with notes, and each battery
device's level over time.

battery_log.csv (semicolon CSV next to devices.csv), one row per event:

    Id       random, so entries made on two synced copies never collide
    Date     YYYY-MM-DD the change happened
    Device   the device's name (as in monitors.csv / devices.csv)
    Event    replaced | charged | checked | note
    Battery  what went in ('2x CR123A', 'built-in Li-ion')
    Level    percent at the time, if known
    Notes    free text
    Source   manual, or auto when a jump in level was detected
    Updated  when the row was last written (sync: the newer edit wins)
    Deleted  'yes' marks a removed entry, so the removal syncs too

battery_history.csv gets a row for a monitored device's first reading and
whenever its reported level changes (Time;Device;Provider;Level;Pct), so it
stays small. A rise
from low to high is logged as an automatic 'replaced' entry. Standard
library only."""
import csv
import datetime as dt
import os
import uuid

LOG_FIELDS = ["Id", "Date", "Device", "Event", "Battery", "Level", "Notes",
              "Source", "Updated", "Deleted"]
HISTORY_FIELDS = ["Time", "Device", "Provider", "Level", "Pct"]
EVENTS = ("replaced", "charged", "checked", "note")
EDITABLE = ("Date", "Device", "Event", "Battery", "Level", "Notes")
# A jump this big, from this low or lower, means fresh batteries.
JUMP, FROM_AT_MOST = 40, 50


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _read(path, fields):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "").strip() for k in fields}
                for r in csv.DictReader(f, delimiter=";")]


def _write(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)
    os.replace(tmp, path)


def load_log(path, include_deleted=False):
    rows = _read(path, LOG_FIELDS)
    return rows if include_deleted else [r for r in rows if r["Deleted"] != "yes"]


def save_log(path, rows):
    _write(path, LOG_FIELDS, sorted(rows, key=lambda r: (r["Date"], r["Updated"])))


def load_history(path):
    return _read(path, HISTORY_FIELDS)


# --------------------------------------------------------------------------- #
# log entries
# --------------------------------------------------------------------------- #
def clean_entry(fields):
    """Validate and normalize user-supplied entry fields; raises ValueError."""
    out = {}
    if "Device" in fields:
        out["Device"] = " ".join(str(fields["Device"]).split())
        if not out["Device"]:
            raise ValueError("device is required")
    if "Event" in fields:
        out["Event"] = str(fields["Event"]).strip().lower() or "replaced"
        if out["Event"] not in EVENTS:
            raise ValueError(f"event must be one of: {', '.join(EVENTS)}")
    if "Date" in fields:
        d = str(fields["Date"]).strip() or dt.date.today().isoformat()
        try:
            out["Date"] = dt.date.fromisoformat(d[:10]).isoformat()
        except ValueError:
            raise ValueError(f"date '{d}' is not YYYY-MM-DD") from None
        if out["Date"] > dt.date.today().isoformat():
            raise ValueError("date is in the future")
    if "Level" in fields:
        lv = str(fields["Level"]).strip().rstrip("%")
        if lv and not (lv.isdigit() and 0 <= int(lv) <= 100):
            raise ValueError("level must be 0-100")
        out["Level"] = lv
    for k in ("Battery", "Notes"):
        if k in fields:
            # one line each: the CSV stays readable and greppable
            out[k] = " ".join(str(fields[k]).split())
    return out


def add_entry(rows, device, event="replaced", date="", battery="", level="", notes="",
              source="manual"):
    """Append a new entry to rows (in memory) and return it."""
    e = {k: "" for k in LOG_FIELDS}
    e.update(clean_entry({"Device": device, "Event": event, "Date": date,
                          "Battery": battery, "Level": level, "Notes": notes}))
    e.update(Id=uuid.uuid4().hex[:12], Source=source, Updated=now_iso())
    rows.append(e)
    return e


def edit_entry(rows, entry_id, fields):
    """Change the editable fields of one entry; returns it, or None."""
    for r in rows:
        if r["Id"] == entry_id and r["Deleted"] != "yes":
            r.update(clean_entry({k: v for k, v in fields.items() if k in EDITABLE}))
            r["Updated"] = now_iso()
            return r
    return None


def delete_entry(rows, entry_id):
    """Mark an entry deleted (a tombstone, so the deletion syncs)."""
    for r in rows:
        if r["Id"] == entry_id and r["Deleted"] != "yes":
            r["Deleted"], r["Updated"] = "yes", now_iso()
            return True
    return False


# --------------------------------------------------------------------------- #
# readings -> history + automatic 'replaced' entries
# --------------------------------------------------------------------------- #
def record_levels(log_rows, history_rows, before, after, now=None):
    """Fold one monitor pass into the history and the log.
    before / after: {device: {'pct', 'label', 'provider'}} from the monitor
    state before and after the pass. Appends a history row per changed level
    and an automatic 'replaced' entry per big rise. Returns (new history
    rows, new log entries)."""
    now = now or now_iso()
    new_hist, new_log = [], []
    known = {h["Device"] for h in history_rows}   # a first reading is always kept
    for dev, b in after.items():
        if not b or b.get("pct") is None:
            continue
        old = (before.get(dev) or {}).get("pct")
        if old == b["pct"] and dev in known:
            continue
        h = {"Time": now, "Device": dev, "Provider": b.get("provider", ""),
             "Level": str(b.get("label") or b["pct"]), "Pct": str(b["pct"])}
        history_rows.append(h)
        new_hist.append(h)
        if old is not None and old <= FROM_AT_MOST and b["pct"] - old >= JUMP:
            new_log.append(add_entry(
                log_rows, dev, "replaced", now[:10], level=str(b["pct"]),
                notes=f"detected: {b.get('provider', '')} level rose from {old}% to {b['pct']}%",
                source="auto"))
    return new_hist, new_log


# --------------------------------------------------------------------------- #
# per-device summary
# --------------------------------------------------------------------------- #
def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def summarize(devices, log_rows, history_rows, levels, today=None):
    """One summary per battery device, worst first.
    devices: names to include (monitored battery devices plus any with log
    entries); levels: {device: {'pct', 'label', 'provider', 'at'}} now."""
    today = today or dt.date.today().isoformat()
    by_dev = {}
    for r in log_rows:
        by_dev.setdefault(r["Device"], []).append(r)
    hist = {}
    for h in history_rows:
        hist.setdefault(h["Device"], []).append(h)
    out = []
    for dev in sorted(set(devices) | set(by_dev)):
        entries = sorted(by_dev.get(dev, []), key=lambda r: r["Date"])
        changes = [r["Date"] for r in entries if r["Event"] in ("replaced", "charged")]
        typ = next((r["Battery"] for r in reversed(entries) if r["Battery"]), "")
        gaps = [_days(a, b) for a, b in zip(changes, changes[1:]) if _days(a, b) > 0]
        avg = round(sum(gaps) / len(gaps)) if gaps else None
        last = changes[-1] if changes else ""
        lv = levels.get(dev) or {}
        next_due = ((dt.date.fromisoformat(last) + dt.timedelta(days=avg)).isoformat()
                    if last and avg else "")
        out.append({
            "device": dev, "pct": lv.get("pct"), "label": lv.get("label", ""),
            "provider": lv.get("provider", ""), "at": lv.get("at", ""),
            "battery": typ, "last_changed": last,
            "days_since": _days(last, today) if last else None,
            "avg_days": avg, "next_due": next_due, "changes": len(changes),
            "entries": len(entries),
            "trend": [{"t": h["Time"], "pct": int(h["Pct"])} for h in hist.get(dev, [])[-60:]
                      if h["Pct"].isdigit()],
        })
    return sorted(out, key=lambda s: (s["pct"] is None, s["pct"] if s["pct"] is not None else 0,
                                      s["device"].lower()))
