"""Offline, read-only checks. No imports/execution of project code or network I/O."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "quant-research"
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "data", "models"}
# Narrow API identifiers, not buy/sell/order words in documentation or variable names.
ORDER_APIS = {
    "create_order", "submit_order", "place_order", "send_order", "SendOrder",
    "cancel_order", "buy_market_order", "sell_market_order", "buy_limit_order",
    "sell_limit_order", "balance_transfer",
}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def python_issues(source: str, label: str) -> list[str]:
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        return [f"{label}:{exc.lineno}: Python syntax: {exc.msg}"]
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def resolved(node):
        name = dotted(node)
        first, *rest = name.split(".")
        return ".".join([aliases.get(first, first), *rest])

    sessions = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            if resolved(node.value.func) in {"requests.Session", "requests.sessions.Session"}:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                sessions.update(dotted(target) for target in targets)
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = resolved(node.func)
        leaf = name.rsplit(".", 1)[-1]
        if leaf in ORDER_APIS:
            issues.append(f"{label}:{node.lineno}: prohibited order API call: {leaf}")
        receiver = dotted(node.func.value) if isinstance(node.func, ast.Attribute) else ""
        request_call = name in {f"requests.{method}" for method in HTTP_METHODS}
        request_call |= receiver in sessions and leaf in HTTP_METHODS
        if request_call:
            timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
            if timeout is None:
                issues.append(f"{label}:{node.lineno}: requests call needs explicit finite timeout")
            else:
                try:
                    value = ast.literal_eval(timeout)
                except (ValueError, TypeError):
                    # Runtime expressions need separate functional review.
                    continue
                values = value if isinstance(value, tuple) else (value,)
                if (isinstance(value, tuple) and len(value) != 2) or any(
                    isinstance(part, bool) or not isinstance(part, (int, float))
                    or not 0 < part < float("inf") for part in values
                ):
                    issues.append(f"{label}:{node.lineno}: timeout must be positive and finite")
    return issues


def content_issues(name: str, source: str) -> list[str]:
    if name.endswith(".py"):
        return python_issues(source, name)
    if name.endswith(".ipynb"):
        try:
            notebook = json.loads(source)
            if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
                return [f"{name}: notebook must contain a cells array"]
            issues = []
            for index, cell in enumerate(notebook["cells"]):
                if not isinstance(cell, dict):
                    issues.append(f"{name}: invalid cell {index + 1}")
                    continue
                if cell.get("cell_type") != "code":
                    continue
                code = cell.get("source", [])
                if isinstance(code, list) and all(isinstance(line, str) for line in code):
                    code = "".join(code)
                if not isinstance(code, str):
                    issues.append(f"{name}: invalid code source in cell {index + 1}")
                    continue
                # Parse Python cells only; magics/shell cells need release review.
                if any(line.lstrip().startswith(("%", "!")) for line in code.splitlines()):
                    issues.append(f"{name}: cell {index + 1} uses IPython magic/shell syntax; "
                                  "export to standalone Python for guard review")
                    continue
                issues.extend(python_issues(code, f"{name}:cell-{index + 1}"))
            return issues
        except (ValueError, TypeError) as exc:
            return [f"{name}: invalid notebook JSON: {exc}"]
    return []


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15).stdout


def selected_sources(root: Path, staged: bool):
    if staged:
        names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        for raw_name in names.split(b"\0"):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8")
            if name.startswith(PROJECT + "/"):
                if Path(name).suffix in {".py", ".ipynb"}:
                    yield name, git(root, "show", f":{name}").decode("utf-8-sig")
                elif Path(name).name == ".env" or Path(name).suffix in {".pem", ".key"}:
                    yield name, None
    else:
        project = root / PROJECT
        if not project.is_dir():
            raise FileNotFoundError(f"Missing project: {project}")
        # Walk without following symlinks or descending into raw data/virtualenvs.
        import os
        for current, directories, files in os.walk(project, followlinks=False):
            directories[:] = [d for d in directories if d not in SKIP
                              and not (Path(current) / d).is_symlink()]
            for filename in files:
                path = Path(current) / filename
                if path.suffix in {".py", ".ipynb"} and not path.is_symlink():
                    yield path.relative_to(root).as_posix(), path.read_text(encoding="utf-8-sig")


def check(root: Path, staged=False):
    issues, count = [], 0
    for name, source in selected_sources(root, staged):
        count += 1
        if source is None:
            issues.append(f"{name}: private credential file must not be committed")
        else:
            issues.extend(content_issues(name, source))
    return issues, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect index blobs, not working copies")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        issues, count = check(args.root.resolve(), args.staged)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"quant-guard could not complete: {exc}", file=sys.stderr)
        return 1
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print(f"quant-guard: {count} source files checked. "
          "Static checks only; strategy/data/Colab runtime remain separate validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
