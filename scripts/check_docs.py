"""Check local Markdown links in published files without network requests."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.md"],
        cwd=ROOT, capture_output=True, encoding="utf-8", check=True,
    )
    files = sorted(set(filter(None, result.stdout.split("\0"))))
    errors = []
    count = 0
    for name in files:
        path = ROOT / name
        if not path.is_file():
            continue  # A tracked file may be deleted in the current change.
        fence = None
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
                continue
            if fence:
                continue
            for match in LINK.finditer(line):
                url = urlsplit(match[1].strip("<>"))
                if url.scheme or url.netloc or not url.path:
                    continue
                target = (path.parent / unquote(url.path)).resolve()
                count += 1
                if not target.exists():
                    errors.append(f"{name}:{line_no}: missing {url.path}")
    for error in errors:
        print(error)
    print(f"Checked {count} local links across {len(files)} Markdown files; {len(errors)} broken.")
    return int(bool(errors))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
