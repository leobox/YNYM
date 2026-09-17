"""Verify the standalone Colab source, generated notebook, and core modules agree."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
COLAB_SOURCE = ROOT / "notebooks" / "pattern_colab.py"
NOTEBOOK = ROOT / "notebooks" / "pattern_top5.ipynb"
MODULES = {
    "hourly_pattern": ROOT / "scanner" / "pattern.py",
    "confirmed_breakout": ROOT / "scanner" / "breakout.py",
    "valid_ohlcv": ROOT / "scanner" / "candle_cleaner.py",
}


def function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise ValueError(f"{path}: missing function {name}")


def normalized(node: ast.FunctionDef) -> str:
    """Ignore deployment-only type hints and docstrings, compare executable bodies."""
    node = ast.fix_missing_locations(ast.parse(ast.unparse(node)).body[0])
    node.decorator_list = []
    node.returns = None
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        arg.annotation = None
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.dump(node, include_attributes=False)


def notebook_code(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    cells = [cell for cell in data.get("cells", []) if cell.get("cell_type") == "code"]
    if len(cells) != 1:
        raise ValueError(f"{path}: expected exactly one code cell, found {len(cells)}")
    if cells[0].get("outputs") or cells[0].get("execution_count") is not None:
        raise ValueError(f"{path}: generated release notebook must not contain saved outputs")
    source = cells[0].get("source", [])
    return "".join(source) if isinstance(source, list) else source


def main() -> int:
    failures = []
    source = COLAB_SOURCE.read_text(encoding="utf-8-sig")
    try:
        if notebook_code(NOTEBOOK) != source:
            failures.append("pattern_top5.ipynb code cell differs from pattern_colab.py")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        failures.append(str(exc))
    for name, module_path in MODULES.items():
        try:
            if normalized(function_node(COLAB_SOURCE, name)) != normalized(function_node(module_path, name)):
                failures.append(f"{name}: executable body differs")
            else:
                print(f"PASS: {name}")
        except (OSError, SyntaxError, ValueError) as exc:
            failures.append(str(exc))
    if failures:
        print("\n".join(f"FAIL: {item}" for item in failures), file=sys.stderr)
        return 1
    print("PASS: notebook code cell matches standalone Python exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
