#!/usr/bin/env python3
"""Render the working tree's changes as one self-contained HTML page.

`git diff` in a terminal is fine for one file and unreadable for twenty. This walks
everything the working tree has changed — modified, added, deleted, untracked — and
writes a single page with a file list, per-file diffs, line numbers and change
counts, so a change set can be reviewed and then thrown away.

    uv run python analysis/experiments/diff_page.py                  # analysis/out/changes.html
    uv run python analysis/experiments/diff_page.py --out /tmp/d.html --open
    uv run python analysis/experiments/diff_page.py --staged          # what is in the index

Nothing here is committed by the tool, and it never touches the index: untracked
files are diffed against an empty file rather than added with `git add -N`, so a
review cannot change what the next commit would contain.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Files to leave out even when they changed: generated, or too noisy to review.
SKIP = (".venv/", "target/", "__pycache__/", "analysis/out/", ".uv-cache/", ".pytest_cache/")

#: How many diff lines a single file may contribute before it is truncated. A file
#: this long is usually generated; the page says so rather than freezing the browser.
MAX_LINES_PER_FILE = 4_000

#: Longest line rendered whole. Minified JavaScript is one line of hundreds of kilobytes.
MAX_LINE_CHARS = 400

#: Order of the page: code first, then its tests, then documents, then data.
ORDER = {"source": 0, "test": 1, "doc": 2, "data": 3, "other": 4}


@dataclass
class FileDiff:
    path: str
    status: str            # "modified", "added", "deleted", "untracked"
    lines: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    binary: bool = False


def git(*args: str, repo: Path = REPO) -> str:
    """Run git and return stdout; a non-zero exit is only an error for the caller to judge."""
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return completed.stdout


def classify(path: str) -> str:
    if "/tests/" in path or path.startswith("tests/") or path.endswith((".mjs", ".js")):
        return "test" if "/tests/" in path else "other"
    if path.startswith("analysis/src/") or path.startswith("src/"):
        return "source"
    if path.endswith((".md", ".toml", ".lock", ".example.toml")):
        return "doc"
    if path.startswith("journal/") or path.endswith(".jsonl"):
        return "data"
    return "other"


def status_of(code: str) -> str:
    return {
        "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
        "C": "copied", "T": "type change", "?": "untracked",
    }.get(code[0] if code[0] != " " else code[1], "modified")


def changed_files(repo: Path, staged: bool) -> list[tuple[str, str]]:
    """`(status, path)` for everything the working tree (or the index) changed."""
    listing = git("diff", "--cached", "--name-status" if staged else "--name-status", repo=repo)
    if staged:
        listing = git("diff", "--cached", "--name-status", repo=repo)
        out = []
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append((status_of(parts[0]), parts[-1]))
        return out

    out: list[tuple[str, str]] = []
    # `-uall` matters: without it git reports an untracked *directory* as one entry, so a
    # new folder full of files would show up as a single line with no diff at all.
    for line in git("status", "--porcelain", "-uall", repo=repo).splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if " -> " in path:                      # a rename: review the destination
            path = path.split(" -> ", 1)[1]
        if any(skip in path for skip in SKIP):
            continue
        if (repo / path).is_dir():              # never a directory: diff files
            continue
        out.append((status_of(code), path))
    return out


def diff_of(status: str, path: str, repo: Path, staged: bool) -> FileDiff:
    """The unified diff for one path, however git has to be asked for it."""
    result = FileDiff(path=path, status=status)
    if staged:
        text = git("diff", "--cached", "--", path, repo=repo)
    elif status == "untracked":
        # Diff against an empty file instead of staging it: reviewing must not change
        # what the next commit would contain.
        completed = subprocess.run(
            ["git", "--no-pager", "diff", "--no-index", "--", "/dev/null", path],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        text = completed.stdout
    else:
        text = git("diff", "HEAD", "--", path, repo=repo)

    if not text:
        return result
    body = text.splitlines()
    result.binary = any(line.startswith("Binary files") for line in body)
    # Drop the git headers: the page draws its own, and they are noise on every file.
    start = next((i for i, line in enumerate(body) if line.startswith("@@") or line.startswith("Binary")), None)
    if start is None:
        return result
    kept = body[start:]
    if len(kept) > MAX_LINES_PER_FILE:
        kept = kept[:MAX_LINES_PER_FILE] + [f"… {len(kept) - MAX_LINES_PER_FILE:,} more lines"]
    result.lines = kept
    result.added = sum(1 for line in kept if line.startswith("+") and not line.startswith("+++"))
    result.removed = sum(1 for line in kept if line.startswith("-") and not line.startswith("---"))
    return result


def render_body(lines: list[str]) -> str:
    """The diff body, with line numbers taken from the hunk headers."""
    out: list[str] = []
    old_line = new_line = 0
    for line in lines:
        marker, old_number, new_number = " ", "", ""
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_line, new_line = int(match.group(1)), int(match.group(2))
            out.append(f'<div class="line hunk"><span class="num"></span><span class="num"></span>'
                       f'<span class="code">{html.escape(line)}</span></div>')
            continue
        if line.startswith("+"):
            marker, new_number = "add", str(new_line)
            new_line += 1
        elif line.startswith("-"):
            marker, old_number = "del", str(old_line)
            old_line += 1
        elif line.startswith("\\"):
            marker = "meta"
        else:
            old_number, new_number = str(old_line), str(new_line)
            old_line += 1
            new_line += 1
        # A minified bundle arrives as a handful of enormous lines; rendering one of them
        # whole makes the page unusable, so say what was elided instead.
        shown = line if len(line) <= MAX_LINE_CHARS else (
            line[:MAX_LINE_CHARS] + f" … [{len(line) - MAX_LINE_CHARS:,} more characters on this line]"
        )
        out.append(
            f'<div class="line {marker}"><span class="num">{old_number}</span>'
            f'<span class="num">{new_number}</span>'
            f'<span class="code">{html.escape(shown) or "&nbsp;"}</span></div>'
        )
    return "".join(out)


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>working tree changes</title>
<style>
  :root {{ --bg:#0e1116; --panel:#141922; --line:#232a36; --text:#d7dde7; --muted:#7c8798;
           --add:#12301f; --add-ink:#7ee2a8; --del:#3a1a1c; --del-ink:#ff9a94; --accent:#4c8dff; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; height:100%; background:var(--bg); color:var(--text);
                font:12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  #app {{ display:flex; height:100%; }}
  aside {{ width:320px; min-width:320px; background:var(--panel); border-right:1px solid var(--line);
           overflow-y:auto; }}
  aside header {{ padding:10px 12px; border-bottom:1px solid var(--line); position:sticky; top:0;
                  background:var(--panel); }}
  aside h1 {{ font-size:13px; margin:0 0 4px; }}
  aside .totals {{ color:var(--muted); font-size:11px; }}
  aside a {{ display:block; padding:6px 12px; color:var(--text); text-decoration:none;
             border-bottom:1px solid #1a2029; }}
  aside a:hover {{ background:#1a2130; }}
  aside a .path {{ display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  aside a .meta {{ color:var(--muted); font-size:11px; }}
  .badge {{ display:inline-block; padding:0 5px; border-radius:4px; font-size:10px;
            border:1px solid var(--line); margin-right:5px; }}
  .badge.modified {{ color:#f5c542; }} .badge.untracked, .badge.added {{ color:var(--add-ink); }}
  .badge.deleted {{ color:var(--del-ink); }}
  main {{ flex:1; overflow:auto; }}
  main > header {{ padding:10px 14px; border-bottom:1px solid var(--line); position:sticky; top:0;
                   background:var(--bg); z-index:2; }}
  main > header .cmd {{ color:var(--muted); }}
  section {{ border-bottom:1px solid var(--line); }}
  section > h2 {{ position:sticky; top:38px; background:#111722; margin:0; padding:8px 14px;
                  font-size:12.5px; border-bottom:1px solid var(--line); z-index:1; }}
  section > h2 .stat {{ color:var(--muted); font-weight:400; }}
  .line {{ display:grid; grid-template-columns:56px 56px 1fr; }}
  .line .num {{ color:#4a5568; text-align:right; padding-right:10px; user-select:none; }}
  .line .code {{ white-space:pre-wrap; word-break:break-word; padding-right:14px; }}
  .line.add {{ background:var(--add); }} .line.add .code {{ color:var(--add-ink); }}
  .line.del {{ background:var(--del); }} .line.del .code {{ color:var(--del-ink); }}
  .line.hunk .code {{ color:var(--accent); }}
  .line.meta .code, .empty {{ color:var(--muted); }}
  .empty {{ padding:10px 14px; }}
</style></head><body>
<div id="app">
  <aside>
    <header>
      <h1>working tree changes</h1>
      <div class="totals">{totals}</div>
    </header>
    {nav}
  </aside>
  <main>
    <header><span class="cmd">{command}</span></header>
    {sections}
  </main>
</div>
</body></html>
"""


def build_page(diffs: list[FileDiff], command: str) -> str:
    nav, sections, total_add, total_del = [], [], 0, 0
    for diff in diffs:
        total_add += diff.added
        total_del += diff.removed
        anchor = "f-" + re.sub(r"[^a-zA-Z0-9]+", "-", diff.path)
        stat = "" if diff.binary else f'<span class="stat"> +{diff.added} −{diff.removed}</span>'
        nav.append(
            f'<a href="#{anchor}"><span class="path">{html.escape(diff.path)}</span>'
            f'<span class="meta"><span class="badge {diff.status}">{diff.status}</span>{stat}</span></a>'
        )
        if diff.binary:
            body = '<div class="empty">binary file — no textual diff</div>'
        elif not diff.lines:
            body = '<div class="empty">no textual changes (mode or metadata only)</div>'
        else:
            body = render_body(diff.lines)
        sections.append(
            f'<section id="{anchor}"><h2>{html.escape(diff.path)}'
            f'<span class="badge {diff.status}">{diff.status}</span>{stat}</h2>{body}</section>'
        )
    return PAGE.format(
        totals=f"{len(diffs)} files · +{total_add} −{total_del}",
        nav="".join(nav),
        sections="".join(sections) or '<div class="empty">the working tree is clean</div>',
        command=html.escape(command),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=str(REPO), help="repository to review (default: this one)")
    parser.add_argument("--out", default=None, help="where to write the page (default: analysis/out/changes.html)")
    parser.add_argument("--staged", action="store_true", help="show what is in the index instead of the working tree")
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    out = Path(args.out) if args.out else repo / "analysis" / "out" / "changes.html"

    files = changed_files(repo, args.staged)
    diffs = [diff_of(status, path, repo, args.staged) for status, path in files]
    diffs.sort(key=lambda diff: (ORDER[classify(diff.path)], diff.path))
    command = "git diff" + (" --cached" if args.staged else "") + " HEAD"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_page(diffs, command))
    added = sum(diff.added for diff in diffs)
    removed = sum(diff.removed for diff in diffs)
    print(f"{len(diffs)} files, +{added} −{removed} -> {out}", flush=True)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
