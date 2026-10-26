#!/usr/bin/env python3
"""Check the public source tree without printing possible secret values.

Only tracked and non-ignored files are inspected. This is a lightweight release
guard, not a substitute for reviewing the staged diff before publication.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {".venv", "local-data", "models", "build", ".build", ".artifacts", "dist", "__pycache__"}
SECRETS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"SESSDATA[=\t ][^\s\"';]{12,}"),
]


def main():
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    files = sorted(set(output.decode().split("\0")) - {""})
    errors = []
    for name in files:
        path = ROOT / name
        if not path.is_file():
            continue
        if (any(part in FORBIDDEN_DIRS for part in Path(name).parts)
                or (path.name.startswith(".env") and path.name != ".env.example")
                or path.name == "cookies.txt" or path.name.endswith(".cookies.txt")):
            errors.append((name, "private/generated file included"))
        if path.stat().st_size > 10 * 1024 * 1024:
            errors.append((name, "file exceeds 10 MiB; review distribution scope"))
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico", ".icns"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append((name, "unexpected binary file"))
            continue
        if any(pattern.search(content) for pattern in SECRETS):
            errors.append((name, "possible credential; inspect locally"))
        if path.suffix == ".json":
            try:
                json.loads(content)
            except ValueError:
                errors.append((name, "invalid JSON"))
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^\n)]+)\)", content):
                target = target.strip("<>")
                if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                target = unquote(target.split("#", 1)[0])
                if target.startswith("/"):
                    errors.append((name, "absolute local documentation link"))
                elif target and not (path.parent / target).exists():
                    errors.append((name, "broken local documentation link: " + target))
    if errors:
        for name, reason in errors:
            print(f"FAIL {name}: {reason}")
        return 1
    print(f"Publication checks passed for {len(files)} files (credentials, scope, JSON, local documentation links).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
