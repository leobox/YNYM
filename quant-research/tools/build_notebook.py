"""Generate the single-cell Colab notebook from the standalone Python source."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "notebooks" / "pattern_colab.py"
TARGET = ROOT / "notebooks" / "pattern_top5.ipynb"


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8-sig")
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "colab": {"name": "pattern_top5.ipynb", "provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "cells": [{"cell_type": "code", "execution_count": None, "metadata": {},
                   "outputs": [], "source": source.splitlines(keepends=True)}],
    }
    TARGET.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Generated {TARGET} from {SOURCE}")


if __name__ == "__main__":
    main()
