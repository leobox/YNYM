"""T-145 v1.0 Frozen Sector Relative-Strength Research Package.

Research and forward-journal evaluation only.
Strictly NO broker connection, account API, or automated order submission.
Existing operational scanner/forward modules remain untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SPEC_TASK_ID = "T-145"
SPEC_VERSION = "1.0"
DEFAULT_FREEZE_REL_PATH = "docs/tasks/T-145-freeze.json"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_json_bytes(obj: object) -> bytes:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verify_spec_freeze(root: Path, freeze_rel_path: str = DEFAULT_FREEZE_REL_PATH) -> dict:
    """Verify that T-145-freeze.json and all 5 frozen specification/evidence files are intact."""
    root = Path(root).resolve()
    freeze_path = root / freeze_rel_path
    if not freeze_path.is_file():
        raise FileNotFoundError(f"SPEC_FREEZE_MISSING:{freeze_path}")

    freeze_doc = json.loads(freeze_path.read_text(encoding="utf-8"))
    if freeze_doc.get("task_id") != SPEC_TASK_ID or freeze_doc.get("spec_version") != SPEC_VERSION:
        raise ValueError("SPEC_FREEZE_IDENTITY_MISMATCH")

    files = freeze_doc.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("SPEC_FREEZE_FILES_EMPTY")

    verified: dict[str, str] = {}
    for rel_path, meta in sorted(files.items()):
        target = root / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"FROZEN_FILE_MISSING:{rel_path}")
        raw = target.read_bytes()
        digest = sha256_bytes(raw)
        normalized_lf = raw.replace(b"\r\n", b"\n")
        digest_lf = sha256_bytes(normalized_lf)
        expected_sha = meta.get("sha256")
        expected_bytes = meta.get("bytes")
        if expected_sha == digest and (expected_bytes is None or len(raw) == int(expected_bytes)):
            verified[rel_path] = digest
        elif expected_sha == digest_lf and (
            expected_bytes is None or len(normalized_lf) == int(expected_bytes)
        ):
            verified[rel_path] = digest_lf
        else:
            raise ValueError(f"FROZEN_HASH_MISMATCH:{rel_path}")

    return {
        "task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
        "freeze_sha256": sha256_file(freeze_path),
        "authoritative_spec": freeze_doc.get("authoritative_spec"),
        "verified_files": verified,
    }
