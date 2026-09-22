"""Dependency/build input fingerprints; standard library only for first setup."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

VERSION = "7.2.0"
ROOT = Path(__file__).resolve().parent.parent
PARTS = ("python", "node", "frontend")


def _files(root, directory, pattern="*"):
    path = root / directory
    return [item for item in path.rglob(pattern) if item.is_file()
            and "__pycache__" not in item.parts
            and not item.relative_to(root).as_posix().startswith("src/generated/")]


def _frontend_files(root):
    files = [root / name for name in ("index.html", "package.json", "package-lock.json")]
    files.extend(_files(root, "src"))
    files.extend(_files(root, "public"))
    files.extend(_files(root, "scripts"))
    for pattern in ("vite.config.*", "tsconfig*.json", "postcss.config.*", "tailwind.config.*"):
        files.extend(root.glob(pattern))
    return files


def _digest(root, files):
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        name = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes() if path.is_file() else b"<missing>"
        # Include names and lengths so deletions, renames and byte boundaries
        # cannot be confused with merely preserved checkout timestamps.
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def input_fingerprints(root=ROOT):
    root = Path(root)
    python = _digest(root, [root / "requirements.txt", root / "requirements.lock.txt"])
    runtime = str(Path(sys.executable).resolve()) + str(sys.version_info[:2])
    return {
        "python": hashlib.sha256((python + runtime).encode()).hexdigest(),
        "node": _digest(root, [root / "package.json", root / "package-lock.json"]),
        "frontend": _digest(root, _frontend_files(root)),
    }


def application_id(root=ROOT):
    root = Path(root)
    inputs = _frontend_files(root) + _files(root, "server", "*.py") + _files(root, "agent_skills", "*.md")
    inputs.extend(root / name for name in ("requirements.txt", "requirements.lock.txt", "launch.py", "start.ps1"))
    return _digest(root, inputs)


# Imported once by the service at startup. A running process must not claim a
# newer checkout merely because source files were edited while it was alive.
BUILD_ID = application_id()


def _saved(root):
    try:
        value = json.loads((root / "data" / "setup-state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def setup_plan(root=ROOT, *, force=False):
    root = Path(root)
    fingerprints, saved = input_fingerprints(root), _saved(root)
    plan = {part: force or saved.get(part) != fingerprints[part] for part in PARTS}
    plan["node"] = plan["node"] or not (root / "node_modules").is_dir()
    plan["frontend"] = plan["frontend"] or plan["node"] or not (root / "dist" / "index.html").is_file()
    return plan


def _write_state(root, saved):
    target = root / "data" / "setup-state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def record_success(part, root=ROOT):
    if part not in PARTS:
        raise ValueError("Unknown setup stage")
    root = Path(root)
    saved = _saved(root)
    saved[part] = input_fingerprints(root)[part]
    _write_state(root, saved)


def invalidate_stage(part, root=ROOT):
    if part not in PARTS:
        raise ValueError("Unknown setup stage")
    root = Path(root)
    saved = _saved(root)
    saved.pop(part, None)
    _write_state(root, saved)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--record", choices=PARTS)
    parser.add_argument("--invalidate", choices=PARTS)
    args = parser.parse_args()
    if args.invalidate:
        invalidate_stage(args.invalidate)
    elif args.record:
        record_success(args.record)
    else:
        print(json.dumps(setup_plan(force=args.setup)))


if __name__ == "__main__":
    main()
