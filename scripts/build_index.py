#!/usr/bin/env python3
"""Regenerate INDEX.md of a recipe data repo: every recipe title A–Z, grouped by first letter.

Usage: python3 build_index.py [DATA_REPO_ROOT]   (stdlib only)
Without an argument the root is the folder above this script, so a copy at
<data-repo>/scripts/build_index.py indexes its own repo (pre-commit hook, Gitea workflow).
The importer image ships this file and passes the root of its working clone.
"""
import re
import sys
import unicodedata
from pathlib import Path


def title_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^title:\s*(.+)$", text, re.M) or re.search(r"^# (.+)$", text, re.M)
    t = m.group(1).strip() if m else path.stem
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1]
    return t


def sort_key(title: str) -> str:
    # ä -> a, é -> e, case-insensitive (same idea as the app's norm())
    return unicodedata.normalize("NFD", title).encode("ascii", "ignore").decode().casefold()


def letter(title: str) -> str:
    c = re.sub(r"^[^a-z0-9]+", "", sort_key(title))[:1]
    return "#" if not c or c.isdigit() else c.upper()


def build(root: Path) -> int:
    recipes = sorted(
        ((title_of(p), p.relative_to(root).as_posix()) for p in (root / "recipes").rglob("*.md")),
        key=lambda r: (sort_key(r[0]), r[1]),  # path breaks ties: same order on every OS
    )
    lines = ["# Rezept-Index", "",
             f"{len(recipes)} Rezepte · wird bei jedem Push automatisch aktualisiert (`scripts/build_index.py`)", ""]
    current = None
    for title, path in recipes:
        if letter(title) != current:
            current = letter(title)
            lines += ["", f"## {current}", ""]
        safe = title.replace("[", r"\[").replace("]", r"\]")
        lines.append(f"- [{safe}]({path})")
    (root / "INDEX.md").write_text("\n".join(lines).replace("\n\n\n", "\n\n") + "\n", encoding="utf-8", newline="\n")
    return len(recipes)


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    if not (root / "recipes").is_dir():
        sys.exit(f"no recipes/ folder in {root}")
    print(f"INDEX.md: {build(root)} recipes")


if __name__ == "__main__":
    main()
