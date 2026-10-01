"""The bench console: see the scores, delete one, bring it back. Works while a
run is going (type a line, it answers between two problems) and on its own
with --manage. A delete never destroys anything: the folder moves to
bench/_archive/<stamp>__<name> and restore moves it back."""

import datetime as dt
import json
import os
import shutil
import sys

from . import state
from .report import build_report

try:
    import msvcrt as _msvcrt
except ImportError:          # not Windows
    _msvcrt = None

ARCHIVE_DIR = os.path.join("bench", "_archive")
CONSOLE_LISTS = {"runs": None, "archive": None}

CONSOLE_HELP = """
Bench commands - type one any time (during a run, or with --manage):
  help             this list
  list             every run, numbered - same order as --report-only
  delete 2         take run 2 out of the report (numbers come from 'list')
  delete NAME      same, by folder or model name (a piece of it is enough)
  archive          the deleted runs, numbered
  restore 1        put archived run 1 back into the report
  status           how many runs and archives you have
  report           the full comparison table (what --report-only prints)
  quit             stop answering commands
A delete moves bench\\<name> to bench\\_archive\\<stamp>__<name>; nothing is
erased, and 'restore' brings it back. The run in progress cannot be deleted.
"""


def _report_key(row):
    summary = row["summary"] or {}
    score = summary.get("score")
    return (summary.get("config", {}).get("scenario", "") or "zz",
            -(score if isinstance(score, (int, float)) else -1.0), row["label"])


def console_entries(archived=False):
    """The run folders (bench/, or bench/_archive/ when asked) with their
    _summary.json contents, in report order: scenario, best score first."""
    root = ARCHIVE_DIR if archived else "bench"
    rows = []
    if not os.path.isdir(root):
        return rows
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        if not archived and os.path.abspath(d) == os.path.abspath(ARCHIVE_DIR):
            continue
        sp = os.path.join(d, "_summary.json")
        summary = None
        if os.path.isfile(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:
                summary = None
        label = (summary or {}).get("name") or (
            entry.split("__", 1)[-1] if archived else entry)
        orig = entry.split("__", 1)[-1] if "__" in entry else entry
        rows.append({"path": d, "folder": entry, "label": label,
                     "orig": orig, "summary": summary})
    if archived:
        rows.sort(key=lambda r: r["folder"])
    else:
        rows.sort(key=_report_key)
    return rows


def console_print_rows(rows, title, active=None, archived=False):
    print(title)
    if not rows:
        print("     (none)")
        return rows
    for num, row in enumerate(rows, 1):
        s = row["summary"] or {}
        score = s.get("score")
        scen = (s.get("config") or {}).get("scenario") or "?"
        n = sum((s.get("counts") or {}).values())
        mark = "*" if active and os.path.abspath(row["path"]) == os.path.abspath(active) else " "
        print(f"  {mark}{num:>3}  {row['label']:<44}"
              f" {('%.1f%%' % score) if isinstance(score, (int, float)) else 'no score':>9}"
              f"  {'fast' if scen in ('exec', 'top', 'fast') else 'slow'} {scen:<9}"
              f" {str(s.get('harness') or '?'):<7} {n if n else '-':>5}"
              f"  {(s.get('sample_hash') or '-')[:12]:<12}"
              f" {(s.get('generated_at') or '')[:10]}")
    if archived:
        print("     restore <number> puts one of these back into the report.")
    else:
        print("     * = the run in progress. delete <number> takes one out of the"
              " report.")
    return rows


def console_list(kind="runs"):
    """Numbered list of the runs (or of the archive), remembered so 'delete 2'
    knows what 2 was."""
    archived = (kind == "archive")
    rows = console_entries(archived=archived)
    CONSOLE_LISTS[kind] = rows
    title = ("Archived runs (restore <number> puts one back):" if archived
             else "Runs in the report:")
    return console_print_rows(rows, title, active=state.CONSOLE_ACTIVE.get("run"),
                              archived=archived)


def console_find(arg, rows, what):
    """One run out of a numbered list: by its number, or by (a piece of) its
    name. Returns (row, None) or (None, complaint)."""
    if not rows:
        return None, (f"there is no {what} to pick from - 'list'"
                      " shows what exists")
    arg = str(arg).strip()
    if arg.isdigit():
        pick = int(arg)
        if 1 <= pick <= len(rows):
            return rows[pick - 1], None
        return None, f"there is no {what} number {pick} (1-{len(rows)} exist)"
    hits = [r for r in rows if r["label"].lower() == arg.lower()
            or r["folder"].lower() == arg.lower()]
    if not hits:
        hits = [r for r in rows if arg.lower() in r["label"].lower()
                or arg.lower() in r["folder"].lower()]
    if not hits:
        return None, f"no {what} matches '{arg}' - 'list' shows them numbered"
    if len(hits) > 1:
        names = ", ".join(r["label"] for r in hits[:6])
        return None, f"'{arg}' matches {len(hits)} of them ({names}) - be exact or use a number"
    return hits[0], None


def archive_run(row, active=None):
    """Move a run folder into bench/_archive. Reversible; never touches the run
    that is being written right now."""
    if active and os.path.abspath(row["path"]) == os.path.abspath(active):
        return None, ("that is the run in progress - it is being written now; stop"
                      " it first")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(ARCHIVE_DIR, f"{stamp}__{row['folder']}")
    step = 0
    while os.path.exists(dest):
        step += 1
        dest = os.path.join(ARCHIVE_DIR, f"{stamp}__{row['folder']}-{step}")
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        shutil.move(row["path"], dest)
    except OSError as exc:
        return None, f"could not move it ({type(exc).__name__}: {exc})"
    return dest, None


def restore_run(row):
    """Move an archived folder back under bench/ under its original name, so it
    counts in the report again."""
    back = os.path.join("bench", row["orig"])
    if os.path.exists(back):
        return None, (f"bench\\{row['label']} already exists - the name is taken;"
                      " delete or rename that one first")
    try:
        shutil.move(row["path"], back)
    except OSError as exc:
        return None, f"could not move it back ({type(exc).__name__}: {exc})"
    return back, None


def refresh_report_files():
    """bench/report.md + .csv rebuilt from what is left, without the table."""
    build_report(show=False)


def bench_command(line, active=None):
    """One console line. False means the console should stop listening."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd, arg = parts[0].lower(), " ".join(parts[1:])
    if cmd in ("help", "?", "h"):
        print(CONSOLE_HELP)
        return True
    if cmd in ("list", "ls", "runs"):
        if arg in ("archive", "archived", "a"):
            console_list("archive")
        else:
            console_list("runs")
        return True
    if cmd in ("archive", "archived"):
        console_list("archive")
        return True
    if cmd in ("delete", "del", "rm", "remove"):
        if not arg:
            print("'delete' needs to know which one: 'list', then 'delete 2' (or"
                  " 'delete <name>').")
            return True
        rows = CONSOLE_LISTS["runs"]
        if rows is None:
            rows = console_list("runs")
        row, complaint = console_find(arg, rows, "run")
        if complaint:
            print(f"     {complaint}")
            return True
        dest, complaint = archive_run(row, active)
        if complaint:
            print(f"     not deleted: {complaint}")
            return True
        print(f"     deleted {row['label']}: moved to {dest}")
        print("     the report is rebuilt without it; 'archive' lists what is"
              " there, 'restore <number>' brings one back.")
        CONSOLE_LISTS["runs"] = None
        refresh_report_files()
        return True
    if cmd in ("restore", "undelete"):
        if not arg:
            console_list("archive")
            print("     'restore <number>' brings one of those back into the report.")
            return True
        rows = CONSOLE_LISTS["archive"]
        if rows is None:
            rows = console_list("archive")
        row, complaint = console_find(arg, rows, "archived run")
        if complaint:
            print(f"     {complaint}")
            return True
        back, complaint = restore_run(row)
        if complaint:
            print(f"     not restored: {complaint}")
            return True
        print(f"     restored {row['label']} -> {back}, it counts in the report"
              " again")
        CONSOLE_LISTS["runs"] = None
        CONSOLE_LISTS["archive"] = None
        refresh_report_files()
        return True
    if cmd in ("report", "table"):
        build_report()
        return True
    if cmd == "status":
        runs = console_entries()
        archives = console_entries(archived=True)
        scored = [r for r in runs if isinstance((r["summary"] or {}).get("score"),
                                               (int, float))]
        print(f"     {len(runs)} run(s) under bench/ ({len(scored)} with a score),"
              f" {len(archives)} archived. Reports: bench/report.md,"
              " bench/report.csv")
        return True
    if cmd in ("quit", "exit", "q"):
        return False
    print(f"     Unknown command '{cmd}'. Type help.")
    return True


def _key_ready():
    """Is a line waiting on the keyboard? Never blocks, and says no when there
    is no keyboard to listen to (piped run, Start-Process, agent chat)."""
    if not state.CONSOLE_LIVE:
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    try:
        if _msvcrt is not None:
            return bool(_msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def console_tick(active=None):
    """One look at the keyboard between two problems: if you typed a bench
    command, it runs now. Costs nothing when nothing was typed."""
    if not _key_ready():
        return
    try:
        line = input("bench> ")
    except EOFError:
        state.CONSOLE_LIVE = False
        print("     (no keyboard to type to - the console is off; --manage opens"
              " it without a run)")
        return
    except KeyboardInterrupt:
        print("\n     (console: Ctrl-C again to stop the run)")
        return
    print(f"bench> {line}")
    if not bench_command(line, active):
        state.CONSOLE_LIVE = False
        print("     console off; 'help' turns it back on (any key, then help)")


def console_loop(active=None):
    """The bench console on its own (--manage): commands until you leave."""
    print(CONSOLE_HELP)
    console_list("runs")
    while True:
        try:
            line = input("bench> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not bench_command(line, active):
            return
