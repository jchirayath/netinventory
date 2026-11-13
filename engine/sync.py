"""Sync the files you edit between two netinventory copies (this Mac and,
say, a Raspberry Pi), keeping edits from both.

Each file has a rule:
  battery_log.csv      by entry Id; the newer 'Updated' wins, and deletions
                       travel as tombstones, so no entry is ever lost
  battery_history.csv  the union of both
  monitors / home / rooms / types / apps .csv
                       three-way merge against the last synced copy: a field
                       changed on one side takes that side's value; changed
                       on both, the side whose file was written last wins
  devices.csv          the same, but only the fields you curate (Device,
                       Notes); scan results (IP, Status...) stay each copy's
                       own, and a sync never deletes a scanned device
  monitor_state.json, events.csv
                       each copy's own

Scanner mode (`sync --scanner`, or sync_scanner = yes): this copy scans and
the other only serves the dashboard (the Pi), so this copy's scan results in
devices.csv, its monitor state (battery levels, who is offline) and its
events replace the other's. Edits still merge both ways.

The last synced state lives in .sync/<target>/ next to the data, per
target. Standard library only."""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request

import store

# name -> (rule, key columns, columns merged (None = all), deletions sync?)
FILES = {
    "battery_log.csv":     ("updated", ("Id",), None, True),
    "battery_history.csv": ("union", ("Time", "Device"), None, False),
    "monitors.csv":        ("merge3", ("Device",), None, True),
    "home.csv":            ("merge3", ("Device", "Name"), None, True),
    "rooms.csv":           ("merge3", ("Room",), None, True),
    "types.csv":           ("merge3", ("Type",), None, True),
    "apps.csv":            ("merge3", ("Key",), None, True),
    "devices.csv":         ("merge3", ("MAC",), ("Device", "Notes"), False),
    # only in scanner mode, from the scanner to the other copy
    "monitor_state.json":  ("push", (), None, False),
    "events.csv":          ("push", (), None, False),
    "app_rooms.csv":       ("push", (), None, False),
}


# --------------------------------------------------------------------------- #
# CSV text <-> (header, rows)
# --------------------------------------------------------------------------- #
def parse(text):
    if not text:
        return [], []
    rd = csv.DictReader(io.StringIO(text), delimiter=";")
    header = rd.fieldnames or []
    return header, [{k: (r.get(k) or "") for k in header} for r in rd]


def dump(header, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, delimiter=";", lineterminator="\n")
    w.writeheader()
    w.writerows({k: r.get(k, "") for k in header} for r in rows)
    return buf.getvalue()


def _key(row, cols):
    # home.csv: a device with no network row is keyed by its Name instead
    if cols == ("Device", "Name"):
        return row.get("Device") or "name:" + row.get("Name", "")
    return tuple(row.get(c, "") for c in cols)


def _header(*hs):
    out = []
    for h in hs:
        out += [c for c in h if c not in out]
    return out


# --------------------------------------------------------------------------- #
# merge rules  (pure: rows in, rows out)
# --------------------------------------------------------------------------- #
def merge_updated(mine, theirs, cols):
    """Per row, the copy with the newer 'Updated' stamp (ISO strings sort)."""
    out = {_key(r, cols): r for r in mine}
    for r in theirs:
        k = _key(r, cols)
        if k not in out or r.get("Updated", "") > out[k].get("Updated", ""):
            out[k] = r
    return list(out.values())


def merge_union(mine, theirs, cols):
    out = {_key(r, cols): r for r in theirs}
    out.update({_key(r, cols): r for r in mine})
    return sorted(out.values(), key=lambda r: tuple(r.get(c, "") for c in cols))


def merge3(base, mine, theirs, cols, fields=None, deletions=True, mine_newer=True,
           scanner=False):
    """Three-way merge. Returns (rows for mine, rows for theirs): the same
    synced fields on both sides, while columns outside `fields` keep each
    side's own value (a device's IP on this Mac vs on the Pi), or with
    scanner=True take mine's on both (the other copy doesn't scan)."""
    B = {_key(r, cols): r for r in base}
    M = {_key(r, cols): r for r in mine}
    T = {_key(r, cols): r for r in theirs}
    out_m, out_t = [], []
    for k in list(M) + [k for k in T if k not in M]:
        b, m, t = B.get(k), M.get(k), T.get(k)
        if m is None or t is None:
            have = m if m is not None else t
            if b is not None and deletions:
                gone_side_unchanged = all(have.get(f, "") == b.get(f, "")
                                          for f in (fields or have.keys()))
                if gone_side_unchanged:
                    continue            # deleted on one side, untouched on the other
            out_m.append(dict(have))    # new on one side, or edited beats deleted
            out_t.append(dict(have))
            continue
        sync = fields or list(dict.fromkeys(list(m) + list(t)))
        merged = {}
        for f in sync:
            mv, tv, bv = m.get(f, ""), t.get(f, ""), (b or {}).get(f, "")
            if mv == tv:
                merged[f] = mv
            elif b is not None and mv == bv:
                merged[f] = tv
            elif b is not None and tv == bv:
                merged[f] = mv
            else:                       # both changed it: the newer file wins
                merged[f] = mv if mine_newer else tv
        out_m.append({**m, **merged})
        out_t.append({**t, **(m if scanner and fields else {}), **merged})
    return out_m, out_t


def merge_file(name, base_text, mine_text, theirs_text, mine_newer=True, scanner=False):
    """(text for mine, text for theirs, text to store as the new base)."""
    rule, cols, fields, deletions = FILES[name]
    if rule == "push":                  # the scanner's copy wins; otherwise untouched
        if scanner and mine_text:
            return mine_text, mine_text, mine_text
        return mine_text, theirs_text, base_text
    hb, base = parse(base_text)
    hm, mine = parse(mine_text)
    ht, theirs = parse(theirs_text)
    header = _header(hm, ht, hb)
    if not header:
        return mine_text, theirs_text, base_text
    if rule == "updated":
        rows = merge_updated(mine, theirs, cols)
        rows.sort(key=lambda r: (r.get("Date", ""), r.get("Updated", "")))
        text = dump(header, rows)
        return text, text, text
    if rule == "union":
        text = dump(header, merge_union(mine, theirs, cols))
        return text, text, text
    rm, rt = merge3(base, mine, theirs, cols, fields, deletions, mine_newer, scanner)
    mine_out = dump(_header(hm or ht, ht), rm) if (hm or rm) else mine_text
    theirs_out = dump(_header(ht or hm, hm), rt) if (ht or rt) else theirs_text
    return mine_out, theirs_out, mine_out


# --------------------------------------------------------------------------- #
# the two copies
# --------------------------------------------------------------------------- #
def export_dir(data_dir):
    """{name: {'text', 'mtime'}} for the sync files present in data_dir."""
    out = {}
    for name in FILES:
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out[name] = {"text": f.read(), "mtime": os.path.getmtime(p)}
    return out


def import_dir(data_dir, files):
    """Write {name: text} into data_dir atomically, only for known files."""
    for name, text in files.items():
        if name not in FILES:
            raise ValueError(f"refusing to write {name!r}")
        store.write_text(os.path.join(data_dir, name), text)


class LocalTarget:
    """Another data directory on this machine (a second copy, or tests)."""

    def __init__(self, path):
        self.path, self.label = os.path.abspath(path), os.path.abspath(path)

    def export(self):
        return export_dir(self.path)

    def import_(self, files):
        import_dir(self.path, files)


class SshTarget:
    """A copy on another machine, reached with ssh (keys, no password
    prompts). It runs that copy's own netinv.py sync-export / sync-import,
    so files are read and written by the same code on both ends."""

    def __init__(self, host, path="netinventory", key=""):
        # never starting with '-': ssh would read it as an option
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._@-]*", host):
            raise ValueError(f"bad ssh host {host!r}")
        self.host, self.path, self.label = host, path, f"{host}:{path}"
        self.key = os.path.expanduser(key) if key else ""

    def _run(self, sub, stdin=None):
        engine = shlex.quote(self.path.rstrip("/") + "/engine")
        cmd = (f"cd {engine} && P=.venv/bin/python; [ -x $P ] || P=python3; "
               f"$P netinv.py {sub}")
        key = ["-i", self.key, "-o", "IdentitiesOnly=yes"] if self.key else []
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *key, "--",
                            self.host, cmd], input=stdin, capture_output=True, text=True,
                           timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"ssh {self.host}: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def export(self):
        return json.loads(self._run("sync-export"))

    def import_(self, files):
        self._run("sync-import", stdin=json.dumps(files))


def _read(path, missing=""):
    if not os.path.exists(path):
        return missing
    with open(path, encoding="utf-8") as f:
        return f.read()


def fingerprint(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def agent_export(data_dir, have=None):
    """The server side of HttpTarget.export: files whose text differs from the
    fingerprints the agent says it holds, and the names of those that match."""
    have = have if isinstance(have, dict) else {}
    files, same = {}, []
    for name, f in export_dir(data_dir).items():
        if have.get(name) == fingerprint(f["text"]):
            same.append(name)
        else:
            files[name] = f
    return {"files": files, "same": same}


class Conflict(Exception):
    """The other copy changed between our export and our import: sync again."""


def agent_import(data_dir, files, expect):
    """The server side of HttpTarget.import_: write `files` only if each is
    still what was exported (`expect`: name -> fingerprint, "" = absent), so an
    edit made on the server in between is never overwritten."""
    if not isinstance(files, dict) or not isinstance(expect, dict):
        raise ValueError("files and expect must be objects")
    for name in files:
        if name not in FILES or not isinstance(files[name], str):
            raise ValueError(f"refusing to write {name!r}")
    _unchanged(data_dir, files, expect)
    import_dir(data_dir, files)


def _unchanged(data_dir, names, expect):
    """Raise Conflict unless each named file still has its expected
    fingerprint ("" = absent)."""
    for name in names:
        now = _read(os.path.join(data_dir, name), None)
        if (fingerprint(now) if now is not None else "") != expect.get(name, ""):
            raise Conflict(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The agent's token must reach only the URL it was given: a redirect
    (to http://, or another host) would carry it along, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"refusing redirect to {newurl}",
                                     headers, fp)


def agent_opener():
    return urllib.request.build_opener(_NoRedirect)


def check_agent_url(url):
    """https only (http just for this machine, in tests): the bearer token
    rides on every request."""
    m = re.fullmatch(r"(https?)://([A-Za-z0-9.-]+)(:\d+)?(/[A-Za-z0-9._/-]*)?", url or "")
    if not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost")):
        raise ValueError(f"agent url must be https://host[/path], not {url!r}")
    return url.rstrip("/")


class HttpTarget:
    """A copy reached over HTTPS: the agent on the Pi calling the server
    (net.aspl.net) at /api/agent/sync with a bearer token. The Pi connects
    out; nothing connects in. Bodies are gzipped JSON, and only files that
    differ from the last synced copy travel."""

    def __init__(self, url, token, timeout=60):
        url = check_agent_url(url)
        if not token:
            raise ValueError("no agent token: netinv agent-token")
        self.url, self.token, self.timeout = url, token, timeout
        self.label, self._seen, self._open = url, {}, agent_opener().open

    def _post(self, body):
        req = urllib.request.Request(
            self.url + "/api/agent/sync", data=gzip.compress(json.dumps(body).encode()),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                     "Content-Encoding": "gzip", "User-Agent": "netinv-agent"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise Conflict(e.read()[:200].decode("utf-8", "replace")) from None
            raise RuntimeError(f"{self.url}: HTTP {e.code} {e.read()[:200]!r}") from None
        return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)

    def export(self, bases=None):
        bases = bases or {}
        got = self._post({"op": "export",
                          "have": {n: fingerprint(t) for n, t in bases.items() if t}})
        files = dict(got.get("files") or {})
        for name in got.get("same") or []:
            if name in FILES and bases.get(name):
                files[name] = {"text": bases[name], "mtime": 0}
        self._seen = {n: fingerprint(f.get("text", "")) for n, f in files.items()}
        return files

    def import_(self, files):
        expect = {n: self._seen.get(n, "") for n in files}
        self._post({"op": "import", "files": files, "expect": expect})


def target(spec, key=""):
    """'user@host[:path]' -> SshTarget (key: an ssh identity file); an
    existing directory -> LocalTarget; an https:// URL -> HttpTarget (key:
    the agent token)."""
    if spec.startswith(("https://", "http://")):
        return HttpTarget(spec, key)
    if os.path.isdir(os.path.expanduser(spec)):
        return LocalTarget(os.path.expanduser(spec))
    host, _, path = spec.partition(":")
    return SshTarget(host, path or "netinventory", key)


def slug(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")[:80] or "target"


def sync(data_dir, tgt, dry_run=False, log=print, scanner=False):
    """Two-way sync of data_dir with tgt (scanner: this copy's scan data and
    monitor state replace tgt's). Returns {name: (mine changed, theirs
    changed)} for the files that differed."""
    base_dir = os.path.join(data_dir, ".sync", slug(tgt.label))
    old = {}
    for name in FILES:
        bp = os.path.join(base_dir, name)
        old[name] = _read(bp)
    mine = export_dir(data_dir)
    # HttpTarget sends only what differs from the last synced copy
    theirs = tgt.export(old) if isinstance(tgt, HttpTarget) else tgt.export()
    to_mine, to_theirs, changed, bases = {}, {}, {}, {}
    for name in FILES:
        m, t = mine.get(name), theirs.get(name)
        if m is None and t is None:
            continue
        bp = os.path.join(base_dir, name)
        base_text = old[name]
        if m is None or t is None:
            # A file missing on one side (a wiped volume, a fresh copy) is
            # copied across. Merged against the old base, every row would look
            # deleted over there and be deleted here too.
            base_text = ""
        mine_newer = (m or {}).get("mtime", 0) >= (t or {}).get("mtime", 0)
        new_m, new_t, new_b = merge_file(name, base_text, (m or {}).get("text", ""),
                                         (t or {}).get("text", ""), mine_newer, scanner)
        cm, ct = new_m != (m or {}).get("text", ""), new_t != (t or {}).get("text", "")
        if cm:
            to_mine[name] = new_m
        if ct:
            to_theirs[name] = new_t
        if cm or ct:
            changed[name] = (cm, ct)
            log(f"  {name}: " + " and ".join(s for s, c in (("this copy", cm),
                                                             (tgt.label, ct)) if c)
                + (" would be updated (dry run)" if dry_run else " updated"))
        bases[bp] = new_b
    if dry_run:
        return changed
    # This copy is checked the way the server checks an agent's import: a file
    # edited here during the round trip (serve runs in another process) makes
    # the sync start over instead of overwriting that edit with the merge.
    expect = {n: fingerprint(f["text"]) for n, f in mine.items()}
    _unchanged(data_dir, to_mine, expect)
    if to_theirs:
        tgt.import_(to_theirs)
    if to_mine:
        agent_import(data_dir, to_mine, expect)
    # Only once both sides hold the merge: a failed write must not make the
    # next sync think unsent edits are already shared.
    # Unchanged bases aren't rewritten: an idle sync writes nothing (SD cards).
    os.makedirs(base_dir, exist_ok=True)
    for bp, text in bases.items():
        if text != old.get(os.path.basename(bp)):
            store.write_text(bp, text)
    return changed
