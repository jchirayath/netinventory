"""Reading and writing the data files, in one place.

Every write goes to a temporary file beside the target, is flushed to disk,
and is renamed over it. A crash, a restart or a power cut mid-write leaves
the old file whole, and another process reading at the same moment (on the
Pi, the agent reads while serve writes) never sees half a file. That matters
beyond the one file: sync would read a truncated file as rows deleted on
this side and delete them on the other copies too.

The CSVs are ';'-separated UTF-8 with a header row. Standard library only."""
import csv
import io
import os


def write_text(path, text, mode=None):
    """Replace path with text atomically; mode (e.g. 0o600) for secrets.
    Without one, the file keeps the permissions it had."""
    tmp = f"{path}.tmp{os.getpid()}"
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
    try:
        with open(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rows_text(fields, rows):
    """The CSV text for rows (dicts; missing fields are blank, extras dropped)."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, delimiter=";", lineterminator="\r\n")
    w.writeheader()
    w.writerows({k: r.get(k, "") for k in fields} for r in rows)
    return out.getvalue()


def write_rows(path, fields, rows):
    write_text(path, rows_text(fields, rows))


def read_rows(path, fields):
    """Rows of a CSV as dicts with exactly these fields, stripped; [] if absent."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "").strip() for k in fields}
                for r in csv.DictReader(f, delimiter=";")]
