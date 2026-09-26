"""One-off refactoring helper: move talkub modules and rewrite every import that points at them.

Usage (from the repo root):

    uv run python scripts/move_modules.py kube=kube.session crypto.age=secrets.age

Each argument is OLD=NEW, both dotted module paths relative to `talkub`. For every pair the
script `git mv`s the file (creating the target package with an `__init__.py` if needed), then
rewrites all of src/ and tests/:

- `from talkub.OLD import x`         -> `from talkub.NEW import x`
- `from talkub[.pkg] import OLD, ...` -> `from talkub.NEW_PARENT import NEW_NAME` (split per package)
- string literals "talkub.OLD[.attr]" -> "talkub.NEW[.attr]" (monkeypatch targets, `-m` entry points)

When the module's own name changes (argoapps -> argocd.app_files), attribute uses `argoapps.x` in
files that imported it as a module are renamed too. Run `ruff check --fix --select I` and
`ruff format` afterwards to re-sort the rewritten imports.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "talkub"

FROM_TALKUB = re.compile(
    r"^(?P<indent>[ \t]*)from talkub(?P<pkg>(?:\.\w+)*) import "
    r"(?:\((?P<multi>[^)]*)\)|(?P<single>[^\n]+))",
    re.MULTILINE,
)


def module_path(dotted: str) -> Path:
    return SRC.joinpath(*dotted.split(".")).with_suffix(".py")


def move_file(old: str, new: str) -> None:
    source, target = module_path(old), module_path(new)
    package = target.parent
    package.mkdir(parents=True, exist_ok=True)
    # Every package level between talkub/ and the target needs an __init__.py.
    level = package
    while level != SRC:
        init = level / "__init__.py"
        if not init.exists():
            init.write_text("")
            subprocess.run(["git", "add", str(init)], cwd=ROOT, check=True)
        level = level.parent
    subprocess.run(["git", "mv", str(source), str(target)], cwd=ROOT, check=True)


def rewrite_strings(text: str, mapping: dict[str, str]) -> str:
    """ "talkub.OLD[.attr]" inside string literals (monkeypatch targets, `-m` entry points)."""
    for old in sorted(mapping, key=len, reverse=True):
        text = re.sub(
            rf"(?<=[\"'])talkub\.{re.escape(old)}(?![\w])",
            f"talkub.{mapping[old]}",
            text,
        )
    return text


def rewrite_imports(text: str, mapping: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Rewrite every `from talkub[.pkg] import ...` statement. If `pkg` itself moved, only the
    path changes. Otherwise each imported name that is a moved module is split out into an import
    from its new package. Returns the new text and the module renames (old local name -> new
    local name) whose attribute uses still need rewriting."""
    renames: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        indent, pkg = match["indent"], match["pkg"].lstrip(".")
        if pkg in mapping:
            return match[0].replace(
                f"talkub.{pkg} import", f"talkub.{mapping[pkg]} import", 1
            )
        body = match["multi"] if match["multi"] is not None else match["single"]
        names = [
            part.strip()
            for line in body.splitlines()
            for part in line.split("#", 1)[0].split(",")
            if part.strip()
        ]
        keep: list[str] = []
        moved: dict[str, list[str]] = {}
        for entry in names:
            name, _, alias = (piece.strip() for piece in entry.partition(" as "))
            full = f"{pkg}.{name}" if pkg else name
            if full not in mapping:
                keep.append(entry)
                continue
            parent, _, new_name = mapping[full].rpartition(".")
            package = f"talkub.{parent}" if parent else "talkub"
            if alias:
                item = new_name if new_name == alias else f"{new_name} as {alias}"
            else:
                item = new_name
                if new_name != name:
                    renames[name] = new_name
            moved.setdefault(package, []).append(item)
        if not moved:
            return match[0]
        lines = []
        if keep:
            source = f"talkub.{pkg}" if pkg else "talkub"
            lines.append(f"{indent}from {source} import {', '.join(keep)}")
        for package, items in moved.items():
            lines.append(f"{indent}from {package} import {', '.join(items)}")
        return "\n".join(lines)

    return FROM_TALKUB.sub(replace, text), renames


def rewrite_file(path: Path, mapping: dict[str, str]) -> None:
    original = path.read_text()
    text, renames = rewrite_imports(original, mapping)
    text = rewrite_strings(text, mapping)
    for old, new in renames.items():
        text = re.sub(rf"(?<![\w.]){re.escape(old)}\.(?=\w)", f"{new}.", text)
    if text != original:
        path.write_text(text)


def main(argv: list[str]) -> int:
    mapping = dict(arg.split("=", 1) for arg in argv)
    for old, new in mapping.items():
        move_file(old, new)
    for path in [*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]:
        rewrite_file(path, mapping)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
