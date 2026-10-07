#!/usr/bin/env python3
"""Make VERSION (repo root) the one place the release version is edited.

    python scripts/sync-version.py           # write VERSION into hub/pyproject.toml and hub/src/acm_hub/__init__.py
    python scripts/sync-version.py --check   # exit 1 if either disagrees (used by CI and the release workflow)

The hub builds from hub/ alone (its Docker context and `pip install` never see the repo root), so the number has to
be present inside hub/ too; this script is what keeps those copies from drifting.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    (ROOT / "hub" / "pyproject.toml", re.compile(r'^(version = ")[^"]*(")', re.M)),
    (ROOT / "hub" / "src" / "acm_hub" / "__init__.py", re.compile(r'^(__version__ = ")[^"]*(")', re.M)),
]


def main(argv: list[str]) -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"VERSION must be plain X.Y.Z, got {version!r}", file=sys.stderr)
        return 1
    check = "--check" in argv
    stale = []
    for path, pattern in TARGETS:
        text = path.read_text(encoding="utf-8")
        new, n = pattern.subn(rf"\g<1>{version}\g<2>", text, count=1)
        if n != 1:
            print(f"no version line found in {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        if new != text:
            stale.append(path.relative_to(ROOT).as_posix())
            if not check:
                path.write_bytes(new.encode("utf-8"))  # bytes, so LF endings are never rewritten
    if check and stale:
        print(f"out of sync with VERSION ({version}): {', '.join(stale)}. Run: python scripts/sync-version.py", file=sys.stderr)
        return 1
    if stale:
        print(f"set {version} in: {', '.join(stale)}")
    else:
        print(f"already at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
