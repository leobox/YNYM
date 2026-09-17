import json
import os
from pathlib import Path
import tempfile
import hashlib
import subprocess

import pandas as pd

def create_manifest(run_id: str, config: dict, results: dict) -> dict:
    revision = None
    dirty = None
    try:
        repo = Path(__file__).resolve().parents[2]
        revision_result = subprocess.run(
            ['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True,
            text=True, timeout=10, check=False)
        if revision_result.returncode == 0:
            revision = revision_result.stdout.strip()
        status = subprocess.run(
            ['git', '-C', str(repo), 'status', '--porcelain', '--', 'quant-research'],
            capture_output=True, text=True, timeout=10, check=False)
        if status.returncode == 0:
            dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        'run_id': run_id,
        'fetched_at': results.get('timestamp'),
        'config': config,
        'metadata': results,
        'code': {'git_revision': revision, 'dirty': dirty},
        'data': {
            'pattern_source': 'Yahoo Finance 60m chart endpoint',
            'quote_source': 'Naver mobile stock endpoints',
            'timezone': 'Asia/Seoul',
            'adjustment_basis': 'not verified',
            'historical_universe': False,
        },
        'version': '1.1'
    }

def save_manifest(manifest: dict, directory: str) -> None:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"manifest_{manifest['run_id']}.json"
    
    with tempfile.NamedTemporaryFile('w', delete=False, dir=directory, suffix='.tmp',
                                     encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
        temp_name = f.name
        
    os.replace(temp_name, target)


def save_snapshot_run(run_id: str, directory: str | Path, tables: dict[str, pd.DataFrame],
                      config: dict, metadata: dict) -> Path:
    """Atomically write all four CSVs followed by their run manifest."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    files = {}
    for label in ('universe', 'scores', 'top5', 'watch'):
        table = tables[label]
        target = path / f'{run_id}_{label}.csv'
        with tempfile.NamedTemporaryFile('w', delete=False, dir=path, suffix='.tmp',
                                         encoding='utf-8-sig', newline='') as handle:
            table.to_csv(handle, index=False)
            temp_name = handle.name
        os.replace(temp_name, target)
        files[target.name] = {
            'rows': len(table),
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    manifest = create_manifest(run_id, config, metadata)
    manifest['files'] = files
    save_manifest(manifest, str(path))
    return path / f'manifest_{run_id}.json'
