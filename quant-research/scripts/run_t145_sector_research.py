"""Offline CLI runner for T-145 v1.0 Frozen Sector Relative-Strength Research (§§8, 9, 10).

Usage examples:
  python quant-research/scripts/run_t145_sector_research.py --verify-freeze
  python quant-research/scripts/run_t145_sector_research.py --bundle path/to/bundle.json --out-dir path/to/runs
  python quant-research/scripts/run_t145_sector_research.py --replay-run path/to/runs/attempt_0001_...

Strictly offline: zero network calls, zero broker/order APIs, non-destructive output registration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

QUANT_RESEARCH_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = QUANT_RESEARCH_ROOT.parent
if str(QUANT_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANT_RESEARCH_ROOT))

from research.sector_v1 import verify_spec_freeze  # noqa: E402
from research.sector_v1.evaluator import (  # noqa: E402
    BOOTSTRAP_BLOCK_LEN,
    BOOTSTRAP_REPS,
    DEFAULT_RANDOM_POLICIES,
    FIXED_RESEARCH_SEED,
    load_research_bundle,
    replay_sealed_evaluation_run,
    run_full_v1_evaluation,
    seal_and_register_evaluation_run,
    seal_pre_run_attempt,
)


def load_offline_bundle(bundle_path: Path, *, strict: bool = True) -> dict:
    """Load a sealed offline research bundle JSON and return boundary-validated inputs."""
    return load_research_bundle(bundle_path, strict=strict)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="T-145 v1.0 Frozen Sector RS Research Offline Runner (No Trading APIs)"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root containing docs/tasks/T-145-freeze.json",
    )
    parser.add_argument(
        "--verify-freeze",
        action="store_true",
        help="Verify SHA256 hashes of all frozen T-145 v1.0 specification files and exit.",
    )
    parser.add_argument(
        "--replay-run",
        type=Path,
        default=None,
        help="Replay a sealed attempt directory without original input paths and verify parity.",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="Path to offline research bundle JSON.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for immutable attempt registry and evaluation artifacts.",
    )
    parser.add_argument(
        "--n-random",
        type=int,
        default=DEFAULT_RANDOM_POLICIES,
        help=(
            "Number of fixed-seed random benchmark policies (frozen v1.0: 1000; "
            "any other count seals the run as DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION)."
        ),
    )
    parser.add_argument(
        "--bootstrap-reps",
        type=int,
        default=BOOTSTRAP_REPS,
        help=(
            "Number of 20d MBB replications (frozen v1.0: 10000; "
            "any other count seals the run as DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION)."
        ),
    )
    parser.add_argument(
        "--allow-diagnostic-conflicts",
        action="store_true",
        help=(
            "Allow diagnostic inspection of inputs with TR/PIT flag conflicts while forcing "
            "effective verification False and INSUFFICIENT_EVIDENCE."
        ),
    )
    args = parser.parse_args(argv)

    freeze_status = verify_spec_freeze(args.repo_root)
    if args.verify_freeze and args.bundle is None and args.replay_run is None:
        print(json.dumps(freeze_status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.replay_run is not None:
        replay_result = replay_sealed_evaluation_run(args.replay_run, repo_root=args.repo_root)
        print(json.dumps(replay_result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.bundle is None or args.out_dir is None:
        parser.error(
            "--bundle and --out-dir are required unless --verify-freeze or --replay-run is used."
        )

    strict_mode = not bool(args.allow_diagnostic_conflicts)
    loaded = load_offline_bundle(args.bundle, strict=strict_mode)

    # 1. Seal inputs, code hashes, config, and raw input snapshot BEFORE running simulations
    pre_sealed = seal_pre_run_attempt(
        out_root=args.out_dir,
        repo_root=args.repo_root,
        validated_inputs=loaded["validated_inputs"],
        eval_start=loaded["eval_start"],
        eval_end=loaded["eval_end"],
        is_uninspected_oos=loaded["is_uninspected_oos"],
        n_random_policies=args.n_random,
        bootstrap_reps=args.bootstrap_reps,
        bootstrap_block_len=BOOTSTRAP_BLOCK_LEN,
        seed=FIXED_RESEARCH_SEED,
        strict_input_validation=strict_mode,
        run_label="t145_v1_0_e0",
    )

    # 2. Execute E0 research simulations and statistical protocol
    report = run_full_v1_evaluation(
        repo_root=args.repo_root,
        calendar=loaded["calendar"],
        mapping=loaded["mapping"],
        bars_by_code=loaded["bars_by_code"],
        cost_schedule=loaded["cost_schedule"],
        provenance=loaded["provenance"],
        eval_start=loaded["eval_start"],
        eval_end=loaded["eval_end"],
        pit_constituents=loaded["pit_constituents"],
        is_uninspected_oos=loaded["is_uninspected_oos"],
        n_random_policies=args.n_random,
        bootstrap_reps=args.bootstrap_reps,
        bootstrap_block_len=BOOTSTRAP_BLOCK_LEN,
        seed=FIXED_RESEARCH_SEED,
        validated_inputs=loaded["validated_inputs"],
        strict_input_validation=strict_mode,
    )

    # 3. Persist ledgers, daily signals, report, and artifact manifest AFTER calculation
    reg_info = seal_and_register_evaluation_run(
        out_root=args.out_dir,
        evaluation_report=report,
        run_label="t145_v1_0_e0",
        validated_inputs=loaded["validated_inputs"],
        pre_sealed_run=pre_sealed,
    )
    summary_out = {
        "attempt_number": reg_info["attempt_number"],
        "run_dir": reg_info["run_dir"],
        "report_path": reg_info["report_path"],
        "report_sha256": reg_info["report_sha256"],
        "artifact_manifest_sha256": reg_info["artifact_manifest_sha256"],
        "evaluation_mode": report["evaluation_mode"],
        "verdict": report["verdict"],
    }
    print(json.dumps(summary_out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
