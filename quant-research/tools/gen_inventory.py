"""Generate an inventory and prove imported result trees match their source bytes."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"D:\DEPO_M\agent\results")
DESTINATION = ROOT / "data" / "imported"
DIRECTORIES = ("hourly_pattern", "hourly_pattern_30d", "clear_trigger",
               "account_hold", "ytd_11am", "pattern_colab")


def tree_manifest(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def tree_hash(manifest: dict[str, tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for name, (size, checksum) in sorted(manifest.items()):
        digest.update(f"{name}\0{size}\0{checksum}\n".encode())
    return digest.hexdigest()


def main() -> int:
    lines = ["# Data Inventory", "",
             "Source: `D:\\DEPO_M\\agent\\results`",
             "Destination: `quant-research/data/imported`", "",
             "File counts include nested files. SHA256 tree hashes cover relative path, size, and file bytes.",
             "No `.env`, API key, credential, `.pem`, or `.key` file was selected for this import.", ""]
    failures = []
    total_files = total_bytes = 0
    for name in DIRECTORIES:
        source_root, destination_root = SOURCE / name, DESTINATION / name
        source_manifest = tree_manifest(source_root)
        destination_manifest = tree_manifest(destination_root)
        identical = source_manifest == destination_manifest
        if not identical:
            failures.append(name)
        size = sum(item[0] for item in destination_manifest.values())
        total_files += len(destination_manifest)
        total_bytes += size
        lines.extend([f"## {name}", "",
                      f"- Files: {len(destination_manifest)}",
                      f"- Bytes: {size}",
                      f"- Tree SHA256: `{tree_hash(destination_manifest)}`",
                      f"- Source copy: {'identical' if identical else 'MISMATCH'}", ""])
        samples = sorted(destination_root.rglob("*.csv"))[:3]
        if samples:
            lines.append("Sample CSVs:")
            for path in samples:
                frame = pd.read_csv(path)
                relative = path.relative_to(destination_root).as_posix()
                lines.append(f"- `{relative}`: {len(frame)} rows; columns: {', '.join(map(str, frame.columns))}")
            lines.append("")
    lines.extend(["## Total", "", f"- Files: {total_files}", f"- Bytes: {total_bytes}", ""])
    (DESTINATION / "INVENTORY.md").write_text("\n".join(lines), encoding="utf-8")
    if failures:
        print(f"Source/destination mismatch: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"PASS: {total_files} files ({total_bytes} bytes) match source trees")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
