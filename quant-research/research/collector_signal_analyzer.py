"""quant-research: quant-collector 전진 관측 신호 분석 및 2차 위험 필터(Meta-Classifier) 연구 모듈

quant-collector/data/resolved_signals.jsonl에 축적된 전진 관측 신호(피처 + 사후 라벨)를
검증·로딩하고, 등록 시각(`registered_at_kst`) 및 완료봉 기준 5분봉 재라벨링, 손익비 매트릭스 분석,
가짜 돌파 방어를 위한 2차 위험 필터(탐색적 가설)를 평가합니다.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class LoadStats:
    file_path: str
    sha256: str
    total_lines: int
    loaded_count: int
    malformed_count: int
    invalid_schema_count: int
    duplicate_count: int
    filtered_strategy_count: int


@dataclass
class ScenarioMetrics:
    scenario_id: str
    sample_count: int
    target_count: int
    stop_count: int
    timeout_count: int
    win_rate: float
    loss_rate: float
    avg_return_pct: float
    ev_pct: float
    profit_factor: Optional[float]
    pending_count: int = 0
    invalid_count: int = 0
    ambiguous_stop_count: int = 0


@dataclass
class FilterResult:
    filter_name: str
    description: str
    total_signals: int
    passed_signals: int
    pass_rate: float
    raw_win_rate: float
    filtered_win_rate: float
    raw_ev_pct: float
    filtered_ev_pct: float
    stopped_false_positives: int
    retained_true_positives: int


def _is_finite_number(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    try:
        num = float(val)
        return math.isfinite(num)
    except (TypeError, ValueError):
        return False


def pass_pattern_filter(features: Dict[str, Any]) -> bool:
    """패턴 유형 검증: 유효한 문자열 패턴이면서 rebound가 아닌 경우만 통과."""
    if not isinstance(features, dict):
        return False
    pt = features.get("pattern_type")
    if not isinstance(pt, str) or not pt.strip():
        return False
    return pt != "rebound"


def pass_goldilocks_atr_filter(features: Dict[str, Any], max_atr: float = 3.80) -> bool:
    """돌파봉 ATR 이격 과열 차단: 결측/비유한값은 미확인으로 차단."""
    if not isinstance(features, dict):
        return False
    ext = features.get("extension_atr")
    if not _is_finite_number(ext):
        return False
    return float(ext) <= max_atr


def pass_min_score_filter(features: Dict[str, Any], min_score: float = 65.0) -> bool:
    """스마트 스코어 하위 차단: 결측/비유한값은 미확인으로 차단."""
    if not isinstance(features, dict):
        return False
    sc = features.get("score")
    if not _is_finite_number(sc):
        return False
    return float(sc) >= min_score


def pass_composite_filter(features: Dict[str, Any]) -> bool:
    """복합 위험 필터: 필수 피처(pattern_type, extension_atr, score) 결측 시 False 반환."""
    return (
        pass_pattern_filter(features)
        and pass_goldilocks_atr_filter(features, max_atr=3.80)
        and pass_min_score_filter(features, min_score=65.0)
    )


pass_meta_filter = pass_composite_filter


class CollectorSignalLoader:
    """quant-collector 전진 관측 신호 로더 및 5분봉 사후 재라벨러."""

    CORE_REQUIRED_KEYS = ("signal_id", "code", "evaluations", "features")
    STRICT_REQUIRED_KEYS = ("signal_id", "code", "signal_time_kst", "evaluations", "features")
    HORIZONS = (3, 5)
    TARGET_PCTS = (3.0, 5.0, 5.35, 7.0, 10.0)
    STOP_PCTS = (3.0, 5.0)

    @staticmethod
    def _eval_key(h: int, t_pct: float, s_pct: float) -> str:
        t_str = "535" if t_pct == 5.35 else str(int(t_pct))
        return f"h{h}_t{t_str}_s{int(s_pct)}"

    @classmethod
    def load_signals_with_stats(
        cls,
        file_path: Path | str,
        strategy_version: Optional[str] = None,
        strict: bool = False,
    ) -> Tuple[List[Dict[str, Any]], LoadStats]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"신호 데이터 파일을 찾을 수 없습니다: {path}")

        raw_bytes = path.read_bytes()
        file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8")

        records: List[Dict[str, Any]] = []
        seen_ids: Dict[str, Dict[str, Any]] = {}
        total_lines = 0
        malformed_count = 0
        invalid_schema_count = 0
        duplicate_count = 0
        filtered_strategy_count = 0

        req_keys = cls.STRICT_REQUIRED_KEYS if strict else cls.CORE_REQUIRED_KEYS

        for line_idx, line in enumerate(text.splitlines(), 1):
            clean_line = line.strip()
            if not clean_line:
                continue
            total_lines += 1
            try:
                record = json.loads(clean_line)
            except json.JSONDecodeError as exc:
                malformed_count += 1
                if strict:
                    raise ValueError(f"손상된 JSON 라인 ({path}:{line_idx}): {exc}") from exc
                continue

            if not isinstance(record, dict):
                invalid_schema_count += 1
                if strict:
                    raise ValueError(f"JSON 레코드가 객체(dict)가 아닙니다 ({path}:{line_idx})")
                continue

            missing = [k for k in req_keys if k not in record or record[k] is None]
            if (
                missing
                or not isinstance(record.get("signal_id"), str)
                or not record["signal_id"].strip()
                or not isinstance(record.get("evaluations"), dict)
                or not isinstance(record.get("features"), dict)
            ):
                invalid_schema_count += 1
                if strict:
                    raise ValueError(
                        f"필수 스키마 키 누락 또는 타입 불일치 ({path}:{line_idx}): missing={missing}"
                    )
                continue

            sig_id = record["signal_id"].strip()
            if sig_id in seen_ids:
                duplicate_count += 1
                if strict:
                    raise ValueError(f"중복된 signal_id 감지 ({path}:{line_idx}): {sig_id}")
                continue
            seen_ids[sig_id] = record

            if strategy_version is not None and record.get("strategy_version") != strategy_version:
                filtered_strategy_count += 1
                continue

            records.append(record)

        stats = LoadStats(
            file_path=str(path),
            sha256=file_sha256,
            total_lines=total_lines,
            loaded_count=len(records),
            malformed_count=malformed_count,
            invalid_schema_count=invalid_schema_count,
            duplicate_count=duplicate_count,
            filtered_strategy_count=filtered_strategy_count,
        )
        return records, stats

    @classmethod
    def load_signals(
        cls,
        file_path: Path | str,
        strategy_version: Optional[str] = None,
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        records, _ = cls.load_signals_with_stats(
            file_path=file_path,
            strategy_version=strategy_version,
            strict=strict,
        )
        return records

    @staticmethod
    def filter_strategy(
        records: List[Dict[str, Any]],
        strategy_version: str = "algorithm260917_v1",
    ) -> List[Dict[str, Any]]:
        return [r for r in records if r.get("strategy_version") == strategy_version]

    @classmethod
    def relabel_from_5m_bars(
        cls,
        records: List[Dict[str, Any]],
        bars_history_dir: Path | str,
        one_way_cost_bps: float = 17.5,
    ) -> List[Dict[str, Any]]:
        """등록 시각(registered_at_kst) 및 신호봉 완료 시각 이후의 5분봉 시가 기준으로 라벨을 재산출합니다."""
        bh_dir = Path(bars_history_dir)
        round_trip_cost_pct = (2.0 * float(one_way_cost_bps)) / 100.0
        relabeled_records: List[Dict[str, Any]] = []

        for rec in records:
            code = str(rec.get("code", "")).zfill(6)
            sig_t_str = rec.get("signal_time_kst")
            reg_t_str = rec.get("registered_at_kst")

            sig_t = pd.Timestamp(sig_t_str, tz="Asia/Seoul") if sig_t_str else None
            reg_t = pd.Timestamp(reg_t_str, tz="Asia/Seoul") if reg_t_str else sig_t

            if sig_t is not None:
                sig_bar_end = sig_t + pd.Timedelta(minutes=30 if sig_t.hour == 15 else 60)
                earliest_entry_ts = max(sig_bar_end, reg_t) if reg_t is not None else sig_bar_end
            else:
                earliest_entry_ts = reg_t

            # 원본 라벨에서 등록 시각 이전에 청산(exit_time < registered_at_kst)된 건 탐지
            raw_evals = rec.get("evaluations", {})
            pre_reg_exit_keys: List[str] = []
            if reg_t is not None and isinstance(raw_evals, dict):
                for k, ev_val in raw_evals.items():
                    if not isinstance(ev_val, dict):
                        continue
                    ex_t_str = ev_val.get("exit_time")
                    if ex_t_str:
                        try:
                            ex_ts = pd.Timestamp(ex_t_str, tz="Asia/Seoul")
                            if ex_ts < reg_t:
                                pre_reg_exit_keys.append(k)
                        except Exception:
                            pass

            f5 = bh_dir / f"{code}_5m.csv"
            if not f5.exists() or earliest_entry_ts is None:
                missing_evals = {
                    cls._eval_key(h, t_pct, s_pct): {
                        "status": "MISSING_BARS",
                        "exit_price": None,
                        "exit_time": None,
                        "return_pct": None,
                    }
                    for h in cls.HORIZONS
                    for t_pct in cls.TARGET_PCTS
                    for s_pct in cls.STOP_PCTS
                }
                relabeled_records.append({
                    **rec,
                    "raw_evaluations": raw_evals,
                    "evaluations": missing_evals,
                    "relabel_meta": {
                        "status": "MISSING_5M_BARS",
                        "pre_registration_exit_in_raw": bool(pre_reg_exit_keys),
                        "pre_registration_exit_keys": pre_reg_exit_keys,
                        "earliest_entry_ts": (
                            earliest_entry_ts.strftime("%Y-%m-%d %H:%M:%S")
                            if earliest_entry_ts is not None
                            else None
                        ),
                        "entry_bar_time": None,
                        "entry_price": None,
                        "one_way_cost_bps": one_way_cost_bps,
                    },
                })
                continue

            df5 = pd.read_csv(f5, index_col=0)
            df5.index = pd.to_datetime(df5.index)
            if df5.index.tz is None:
                df5.index = df5.index.tz_localize("Asia/Seoul")
            else:
                df5.index = df5.index.tz_convert("Asia/Seoul")
            df5 = df5[~df5.index.duplicated()].sort_index()

            future_df = df5[df5.index >= earliest_entry_ts]
            tradable_df = future_df[future_df["Volume"] > 0]
            if tradable_df.empty:
                nofill_evals = {
                    cls._eval_key(h, t_pct, s_pct): {
                        "status": "NO_FILL",
                        "exit_price": None,
                        "exit_time": None,
                        "return_pct": None,
                    }
                    for h in cls.HORIZONS
                    for t_pct in cls.TARGET_PCTS
                    for s_pct in cls.STOP_PCTS
                }
                relabeled_records.append({
                    **rec,
                    "raw_evaluations": raw_evals,
                    "evaluations": nofill_evals,
                    "relabel_meta": {
                        "status": "NO_FILL",
                        "pre_registration_exit_in_raw": bool(pre_reg_exit_keys),
                        "pre_registration_exit_keys": pre_reg_exit_keys,
                        "earliest_entry_ts": earliest_entry_ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_bar_time": None,
                        "entry_price": None,
                        "one_way_cost_bps": one_way_cost_bps,
                    },
                })
                continue

            entry_bar_ts = tradable_df.index[0]
            entry_open = float(tradable_df.iloc[0]["Open"])
            path_df = df5[df5.index >= entry_bar_ts]
            observed_dates = sorted({ts.strftime("%Y-%m-%d") for ts in path_df.index})

            new_evals: Dict[str, Dict[str, Any]] = {}
            for h in cls.HORIZONS:
                for t_pct in cls.TARGET_PCTS:
                    for s_pct in cls.STOP_PCTS:
                        key = cls._eval_key(h, t_pct, s_pct)
                        target_p = entry_open * (1.0 + t_pct / 100.0)
                        stop_p = entry_open * (1.0 - s_pct / 100.0)

                        status = "PENDING"
                        exit_price: Optional[float] = None
                        exit_time: Optional[str] = None
                        ret_pct: Optional[float] = None

                        for b_idx, (ts, bar) in enumerate(path_df.iterrows()):
                            b_date = ts.strftime("%Y-%m-%d")
                            day_rank = observed_dates.index(b_date) + 1
                            if day_rank > h:
                                prev_ts = path_df.index[b_idx - 1]
                                prev_bar = path_df.iloc[b_idx - 1]
                                status = "TIMEOUT"
                                exit_price = float(prev_bar["Close"])
                                exit_time = prev_ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = (exit_price / entry_open - 1.0) * 100.0
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break

                            if float(bar["Volume"]) <= 0:
                                continue

                            b_open = float(bar["Open"])
                            b_high = float(bar["High"])
                            b_low = float(bar["Low"])

                            hit_stop = b_low <= stop_p or b_open <= stop_p
                            hit_target = b_high >= target_p or b_open >= target_p

                            if b_open <= stop_p:
                                status = "STOP_FIRST"
                                exit_price = b_open
                                exit_time = ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = (exit_price / entry_open - 1.0) * 100.0
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break
                            elif b_open >= target_p:
                                status = "TARGET_FIRST"
                                exit_price = target_p
                                exit_time = ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = t_pct
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break

                            if hit_stop and hit_target:
                                status = "AMBIGUOUS_STOP"
                                exit_price = stop_p
                                exit_time = ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = -s_pct
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break
                            elif hit_stop:
                                status = "STOP_FIRST"
                                exit_price = stop_p
                                exit_time = ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = -s_pct
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break
                            elif hit_target:
                                status = "TARGET_FIRST"
                                exit_price = target_p
                                exit_time = ts.strftime("%Y-%m-%d %H:%M")
                                gross_ret = t_pct
                                ret_pct = round(gross_ret - round_trip_cost_pct, 3)
                                break

                        if status == "PENDING" and len(observed_dates) >= h:
                            last_ts = path_df.index[-1]
                            last_bar = path_df.iloc[-1]
                            status = "TIMEOUT"
                            exit_price = float(last_bar["Close"])
                            exit_time = last_ts.strftime("%Y-%m-%d %H:%M")
                            gross_ret = (exit_price / entry_open - 1.0) * 100.0
                            ret_pct = round(gross_ret - round_trip_cost_pct, 3)

                        new_evals[key] = {
                            "status": status,
                            "exit_price": round(exit_price, 2) if exit_price is not None else None,
                            "exit_time": exit_time,
                            "return_pct": ret_pct,
                        }

            ref_p = rec.get("entry_reference_price")
            slip_vs_ref = (
                round((entry_open / float(ref_p) - 1.0) * 100.0, 3)
                if _is_finite_number(ref_p) and float(ref_p) > 0
                else None
            )
            relabeled_records.append({
                **rec,
                "raw_evaluations": raw_evals,
                "evaluations": new_evals,
                "relabel_meta": {
                    "status": "OK",
                    "pre_registration_exit_in_raw": bool(pre_reg_exit_keys),
                    "pre_registration_exit_keys": pre_reg_exit_keys,
                    "earliest_entry_ts": earliest_entry_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_bar_time": entry_bar_ts.strftime("%Y-%m-%d %H:%M"),
                    "entry_price": entry_open,
                    "entry_slippage_vs_ref_pct": slip_vs_ref,
                    "one_way_cost_bps": one_way_cost_bps,
                },
            })

        return relabeled_records


class PayoffMatrixAnalyzer:
    """목표가/손절가/보유기간 시나리오 매트릭스 분석기."""

    SCENARIO_KEYS = [
        "h3_t3_s3", "h3_t3_s5", "h3_t5_s3", "h3_t5_s5",
        "h3_t7_s3", "h3_t7_s5", "h3_t10_s3", "h3_t10_s5",
        "h5_t3_s3", "h5_t3_s5", "h5_t5_s3", "h5_t5_s5",
        "h5_t7_s3", "h5_t7_s5", "h5_t10_s3", "h5_t10_s5",
    ]

    EXTENDED_SCENARIO_KEYS = [
        "h3_t3_s3", "h3_t3_s5", "h3_t5_s3", "h3_t5_s5",
        "h3_t535_s3", "h3_t535_s5",
        "h3_t7_s3", "h3_t7_s5", "h3_t10_s3", "h3_t10_s5",
        "h5_t3_s3", "h5_t3_s5", "h5_t5_s3", "h5_t5_s5",
        "h5_t535_s3", "h5_t535_s5",
        "h5_t7_s3", "h5_t7_s5", "h5_t10_s3", "h5_t10_s5",
    ]

    COMPLETED_STATUSES = ("TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_STOP", "TIMEOUT")

    @classmethod
    def evaluate_scenario(cls, records: List[Dict[str, Any]], scenario_key: str) -> ScenarioMetrics:
        target_cnt = 0
        stop_cnt = 0
        ambiguous_cnt = 0
        timeout_cnt = 0
        pending_cnt = 0
        invalid_cnt = 0
        returns: List[float] = []
        gross_profit = 0.0
        gross_loss = 0.0

        for r in records:
            evals = r.get("evaluations", {})
            if not isinstance(evals, dict):
                invalid_cnt += 1
                continue
            sc_data = evals.get(scenario_key)
            if not isinstance(sc_data, dict):
                continue

            status = sc_data.get("status")
            if status == "PENDING":
                pending_cnt += 1
                continue
            if status not in cls.COMPLETED_STATUSES:
                invalid_cnt += 1
                continue

            ret_raw = sc_data.get("return_pct")
            if not _is_finite_number(ret_raw):
                invalid_cnt += 1
                continue

            ret = float(ret_raw)
            returns.append(ret)

            if status == "TARGET_FIRST":
                target_cnt += 1
            elif status in ("STOP_FIRST", "AMBIGUOUS_STOP"):
                stop_cnt += 1
                if status == "AMBIGUOUS_STOP":
                    ambiguous_cnt += 1
            elif status == "TIMEOUT":
                timeout_cnt += 1

            if ret > 0:
                gross_profit += ret
            elif ret < 0:
                gross_loss += abs(ret)

        total_cnt = len(returns)
        win_rate = (target_cnt / total_cnt * 100.0) if total_cnt > 0 else 0.0
        loss_rate = (stop_cnt / total_cnt * 100.0) if total_cnt > 0 else 0.0
        avg_ret = (sum(returns) / total_cnt) if total_cnt > 0 else 0.0
        ev = avg_ret

        if gross_loss > 0:
            pf_val: Optional[float] = round(gross_profit / gross_loss, 2)
        else:
            pf_val = None

        return ScenarioMetrics(
            scenario_id=scenario_key,
            sample_count=total_cnt,
            target_count=target_cnt,
            stop_count=stop_cnt,
            timeout_count=timeout_cnt,
            win_rate=round(win_rate, 2),
            loss_rate=round(loss_rate, 2),
            avg_return_pct=round(avg_ret, 3),
            ev_pct=round(ev, 3),
            profit_factor=pf_val,
            pending_count=pending_cnt,
            invalid_count=invalid_cnt,
            ambiguous_stop_count=ambiguous_cnt,
        )

    @classmethod
    def evaluate_all(
        cls,
        records: List[Dict[str, Any]],
        include_extended: bool = False,
    ) -> Dict[str, ScenarioMetrics]:
        keys = cls.EXTENDED_SCENARIO_KEYS if include_extended else cls.SCENARIO_KEYS
        return {key: cls.evaluate_scenario(records, key) for key in keys}


class RiskFilterEngine:
    """가짜 돌파 차단을 위한 2차 위험 필터(Meta-Classifier) 평가 엔진."""

    @staticmethod
    def _is_valid_completed(ev: Dict[str, Any]) -> bool:
        if not isinstance(ev, dict):
            return False
        st = ev.get("status")
        return st in PayoffMatrixAnalyzer.COMPLETED_STATUSES and _is_finite_number(ev.get("return_pct"))

    @classmethod
    def get_feature_summary(
        cls,
        records: List[Dict[str, Any]],
        scenario_key: str = "h5_t5_s5",
    ) -> Dict[str, Any]:
        """성공군(TARGET_FIRST)과 실패군(STOP_FIRST/AMBIGUOUS_STOP)의 피처 통계 요약."""
        targets: List[Dict[str, Any]] = []
        stops: List[Dict[str, Any]] = []

        for r in records:
            ev = r.get("evaluations", {}).get(scenario_key, {})
            if not cls._is_valid_completed(ev):
                continue
            status = ev.get("status")
            feat = r.get("features", {})
            if not isinstance(feat, dict) or not feat:
                continue
            if status == "TARGET_FIRST":
                targets.append(feat)
            elif status in ("STOP_FIRST", "AMBIGUOUS_STOP"):
                stops.append(feat)

        numeric_fields = ["score", "extension_atr", "volume_ratio", "trigger_ratio", "trigger_amount_e8"]
        summary: Dict[str, Any] = {
            "target_count": len(targets),
            "stop_count": len(stops),
            "features": {},
            "pattern_type_dist": {
                "TARGET_FIRST": {},
                "STOP_FIRST": {},
            },
        }

        for f in numeric_fields:
            t_vals = [float(x[f]) for x in targets if _is_finite_number(x.get(f))]
            s_vals = [float(x[f]) for x in stops if _is_finite_number(x.get(f))]
            summary["features"][f] = {
                "target_mean": round(sum(t_vals) / len(t_vals), 2) if t_vals else None,
                "stop_mean": round(sum(s_vals) / len(s_vals), 2) if s_vals else None,
            }

        for x in targets:
            pt = x.get("pattern_type") or "unknown"
            summary["pattern_type_dist"]["TARGET_FIRST"][pt] = (
                summary["pattern_type_dist"]["TARGET_FIRST"].get(pt, 0) + 1
            )
        for x in stops:
            pt = x.get("pattern_type") or "unknown"
            summary["pattern_type_dist"]["STOP_FIRST"][pt] = (
                summary["pattern_type_dist"]["STOP_FIRST"].get(pt, 0) + 1
            )

        return summary

    @classmethod
    def evaluate_filter(
        cls,
        records: List[Dict[str, Any]],
        filter_fn: Callable[[Dict[str, Any]], bool],
        filter_name: str,
        description: str,
        scenario_key: str = "h5_t5_s5",
    ) -> FilterResult:
        """단일 필터 규칙의 완료 표본 기준 사전 차단 효과 및 승률/기대값 변화 검증."""
        completed_records = [
            r for r in records
            if cls._is_valid_completed(r.get("evaluations", {}).get(scenario_key, {}))
        ]
        raw_metrics = PayoffMatrixAnalyzer.evaluate_scenario(completed_records, scenario_key)

        passed_records = [r for r in completed_records if filter_fn(r.get("features", {}))]
        filtered_metrics = PayoffMatrixAnalyzer.evaluate_scenario(passed_records, scenario_key)

        blocked_records = [r for r in completed_records if not filter_fn(r.get("features", {}))]
        stopped_false_positives = sum(
            1 for r in blocked_records
            if r.get("evaluations", {}).get(scenario_key, {}).get("status") in ("STOP_FIRST", "AMBIGUOUS_STOP")
        )
        retained_true_positives = sum(
            1 for r in passed_records
            if r.get("evaluations", {}).get(scenario_key, {}).get("status") == "TARGET_FIRST"
        )

        total_sig = raw_metrics.sample_count
        pass_cnt = filtered_metrics.sample_count
        pass_rate = round(pass_cnt / total_sig * 100.0, 1) if total_sig > 0 else 0.0

        return FilterResult(
            filter_name=filter_name,
            description=description,
            total_signals=total_sig,
            passed_signals=pass_cnt,
            pass_rate=pass_rate,
            raw_win_rate=raw_metrics.win_rate,
            filtered_win_rate=filtered_metrics.win_rate,
            raw_ev_pct=raw_metrics.ev_pct,
            filtered_ev_pct=filtered_metrics.ev_pct,
            stopped_false_positives=stopped_false_positives,
            retained_true_positives=retained_true_positives,
        )


def build_default_filters() -> List[tuple[str, str, Callable[[Dict[str, Any]], bool]]]:
    """기본 탐색 가설 기반 필터 목록 생성 (결측/비유한 피처는 모두 False 처리)."""
    return [
        (
            "PatternTypeFilter",
            "rebound(낙폭과대 반등) 제외, 유효 패턴(base_breakout 등)만 허용",
            pass_pattern_filter,
        ),
        (
            "GoldilocksAtrFilter",
            "돌파봉 ATR 이격 과열 차단 (extension_atr <= 3.80, 결측 제외)",
            pass_goldilocks_atr_filter,
        ),
        (
            "MinScoreFilter",
            "스마트 스코어 하위 차단 (score >= 65.0, 결측 제외)",
            pass_min_score_filter,
        ),
        (
            "CompositeFilter",
            "복합 필터: rebound 제외 AND extension_atr <= 3.80 AND score >= 65.0 (결측 제외)",
            pass_composite_filter,
        ),
    ]


def _format_target_pct(sc_id: str) -> str:
    raw_t = sc_id.split("_")[1][1:]
    if raw_t == "535":
        return "+5.35%"
    return f"+{raw_t}%"


def generate_analysis_report(records: List[Dict[str, Any]], scenario_key: str = "h5_t5_s5") -> str:
    """입력 레코드로부터 모든 수치와 해석을 동적으로 생성하는 Markdown 리포트."""
    has_extended = any(
        isinstance(r.get("evaluations"), dict) and "h5_t535_s5" in r["evaluations"]
        for r in records
    )
    matrix = PayoffMatrixAnalyzer.evaluate_all(records, include_extended=has_extended)
    feat_summary = RiskFilterEngine.get_feature_summary(records, scenario_key)
    filters = build_default_filters()

    filter_results = [
        RiskFilterEngine.evaluate_filter(records, fn, name, desc, scenario_key)
        for name, desc, fn in filters
    ]

    base_metric = matrix.get(scenario_key) or PayoffMatrixAnalyzer.evaluate_scenario(records, scenario_key)
    base_sample_cnt = base_metric.sample_count
    pending_cnt = base_metric.pending_count
    invalid_cnt = base_metric.invalid_count

    strat_versions = sorted({str(r.get("strategy_version") or "unspecified") for r in records})
    strat_str = ", ".join(f"`{v}`" for v in strat_versions) if strat_versions else "`none`"

    relabeled_cnt = sum(1 for r in records if isinstance(r.get("relabel_meta"), dict) and r["relabel_meta"].get("status") == "OK")
    pre_reg_cnt = sum(
        1 for r in records
        if isinstance(r.get("relabel_meta"), dict) and r["relabel_meta"].get("pre_registration_exit_in_raw")
    )

    lines: List[str] = [
        "# 🔬 quant-collector 전진 관측 신호 분석 및 2차 위험 필터(탐색 가설) 리포트",
        "",
        f"> **입력 레코드**: 총 `{len(records)}건` | **완료 유효 표본(`{scenario_key}`)**: `{base_sample_cnt}건` "
        f"(진행중 `{pending_cnt}건`, 결측/제외 `{invalid_cnt}건`) | **전략 버전**: {strat_str}",
    ]
    if relabeled_cnt > 0 or pre_reg_cnt > 0:
        lines.append(
            f"> **시점 정합성 교정**: 5분봉 등록 이후 재라벨링 완료 `{relabeled_cnt}건` | "
            f"원본 라벨 중 등록 시각(`registered_at_kst`) 이전 청산 오류 감지 `{pre_reg_cnt}건`"
        )
    lines.extend([
        "",
        "---",
        "",
        "## 1. 손익비 & 보유기간 매트릭스 그리드 성과 (Payoff Matrix)",
        "",
        "| 시나리오 | 보유 기간 | 목표 익절 | 손절 기준 | 완료 표본 | 익절(승) | 손절(패) | 만기 | 확정 승률 | 평균 수익률 (EV) | 손익계수(PF) |",
        "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ])

    for sc_id, m in matrix.items():
        h = "3일" if sc_id.startswith("h3") else "5일"
        t = _format_target_pct(sc_id)
        s = f"-{sc_id.split('_')[2][1:]}%"
        pf_str = f"{m.profit_factor:.2f}" if m.profit_factor is not None else "N/A"
        lines.append(
            f"| `{m.scenario_id}` | {h} | {t} | {s} | {m.sample_count}건 | {m.target_count}건 | "
            f"{m.stop_count}건 | {m.timeout_count}건 | **{m.win_rate:.1f}%** | **{m.ev_pct:+.3f}%** | {pf_str} |"
        )

    # 동적 매트릭스 요약 생성 (하드코딩 배제)
    valid_metrics = [m for m in matrix.values() if m.sample_count > 0]
    lines.extend(["", "### 💡 손익비 매트릭스 동적 요약"])
    if valid_metrics:
        best_ev_m = max(valid_metrics, key=lambda x: x.ev_pct)
        best_wr_m = max(valid_metrics, key=lambda x: x.win_rate)
        h5_t3_s5 = matrix.get("h5_t3_s5")
        h5_t5_s5 = matrix.get("h5_t5_s5")
        h5_t7_s5 = matrix.get("h5_t7_s5")
        lines.append(
            f"1. **최고 기대값(EV) 시나리오**: `{best_ev_m.scenario_id}` (완료 `{best_ev_m.sample_count}건`, "
            f"승률 `{best_ev_m.win_rate:.1f}%`, 평균 수익률 `{best_ev_m.ev_pct:+.3f}%`)."
        )
        lines.append(
            f"2. **최고 승률 시나리오**: `{best_wr_m.scenario_id}` (승률 `{best_wr_m.win_rate:.1f}%`, "
            f"평균 수익률 `{best_wr_m.ev_pct:+.3f}%`)."
        )
        if h5_t3_s5 and h5_t5_s5 and h5_t7_s5 and h5_t5_s5.sample_count > 0:
            lines.append(
                f"3. **5일 보유(-5% 손절) 목표치별 비교**: `h5_t3_s5` 승률 `{h5_t3_s5.win_rate:.1f}%`(EV `{h5_t3_s5.ev_pct:+.3f}%`), "
                f"`h5_t5_s5` 승률 `{h5_t5_s5.win_rate:.1f}%`(EV `{h5_t5_s5.ev_pct:+.3f}%`), "
                f"`h5_t7_s5` 승률 `{h5_t7_s5.win_rate:.1f}%`(EV `{h5_t7_s5.ev_pct:+.3f}%`)."
            )
    else:
        lines.append("1. 완료된 유효 표본이 없어 매트릭스 요약을 산출할 수 없습니다.")

    lines.extend([
        "",
        "---",
        "",
        f"## 2. 피처 변별력 분석 (Feature Separation: `{scenario_key}` 완료 표본 기준)",
        "",
        f"- **익절 달성군 (TARGET_FIRST)**: `{feat_summary['target_count']}건`",
        f"- **손절 접촉군 (STOP_FIRST / AMBIGUOUS_STOP)**: `{feat_summary['stop_count']}건`",
        "",
        "| 피처 (Feature) | 익절군 평균 | 손절군 평균 | 피처 설명 |",
        "|:---|:---:|:---:|:---|",
    ])

    feat_desc = {
        "score": "스마트 종합 스코어 (모멘텀·수축·거래량 결합)",
        "extension_atr": "돌파 시점 ATR 이격도 (과열 지표)",
        "volume_ratio": "거래량 배수 (VR)",
        "trigger_ratio": "돌파봉 거래대금 중앙값 대비 배수",
        "trigger_amount_e8": "돌파봉 거래대금 (억원)",
    }

    for f_name, f_stats in feat_summary["features"].items():
        t_m = f_stats["target_mean"]
        s_m = f_stats["stop_mean"]
        t_str = f"{t_m}" if t_m is not None else "-"
        s_str = f"{s_m}" if s_m is not None else "-"
        desc = feat_desc.get(f_name, "")
        lines.append(f"| **`{f_name}`** | {t_str} | {s_str} | {desc} |")

    t_dist = feat_summary["pattern_type_dist"]["TARGET_FIRST"]
    s_dist = feat_summary["pattern_type_dist"]["STOP_FIRST"]
    t_dist_str = ", ".join([f"`{k}`: {v}건" for k, v in sorted(t_dist.items())]) or "없음"
    s_dist_str = ", ".join([f"`{k}`: {v}건" for k, v in sorted(s_dist.items())]) or "없음"

    rebound_t = t_dist.get("rebound", 0)
    rebound_s = s_dist.get("rebound", 0)
    bb_t = t_dist.get("base_breakout", 0)
    bb_s = s_dist.get("base_breakout", 0)

    lines.extend([
        "",
        "### 🏷️ 패턴 유형(Pattern Type)별 승패 분포",
        f"- **익절군(TARGET_FIRST)**: {t_dist_str}",
        f"- **손절군(STOP_FIRST)**: {s_dist_str}",
        "",
        f"> **패턴별 집계(`{scenario_key}`)**: `base_breakout` 익절 `{bb_t}건` / 손절 `{bb_s}건` | "
        f"`rebound` 익절 `{rebound_t}건` / 손절 `{rebound_s}건` (만기 종료 건은 위 익절/손절 양쪽에서 제외).",
        "",
        "---",
        "",
        f"## 3. 2차 위험 필터(Meta-Classifier) 탐색 가설 검증 (`{scenario_key}`)",
        "",
        "| 필터 명칭 | 필터 조건 및 설명 | 통과율 (통과/완료표본) | 기존 승률 → 필터 후 승률 | 기존 EV → 필터 후 EV | 차단된 손절 | 보존된 익절 |",
        "|:---|:---|:---:|:---:|:---:|:---:|:---:|",
    ])

    for fr in filter_results:
        lines.append(
            f"| **`{fr.filter_name}`** | {fr.description} | {fr.pass_rate:.1f}% ({fr.passed_signals}/{fr.total_signals}) | "
            f"{fr.raw_win_rate:.1f}% → **{fr.filtered_win_rate:.1f}%** | "
            f"{fr.raw_ev_pct:+.3f}% → **{fr.filtered_ev_pct:+.3f}%** | "
            f"**{fr.stopped_false_positives}건** | **{fr.retained_true_positives}건** |"
        )

    lines.extend([
        "",
        "### 🏆 검증에 사용된 복합 위험 필터 구현 (`pass_composite_filter`)",
        "```python",
        "def pass_composite_filter(features: dict) -> bool:",
        "    \"\"\"필수 피처(pattern_type, extension_atr, score) 결측/비유한값은 False로 차단.\"\"\"",
        "    return (",
        "        pass_pattern_filter(features)",
        "        and pass_goldilocks_atr_filter(features, max_atr=3.80)",
        "        and pass_min_score_filter(features, min_score=65.0)",
        "    )",
        "```",
        "",
        "---",
        "",
        "## 4. 탐색적 한계 및 연구 규율 고지",
        f"1. **소수 표본 탐색 가설 고지**: 현재 분석은 완료 표본 `{base_sample_cnt}건`에 기반한 **탐색적 가설(Exploratory Hypothesis)**이며, 확정된 미래 실전 확률이나 다른 타임프레임(일봉 등)의 자동 매수 금지 조건으로 일반화할 수 없습니다.",
        "2. **시점 및 체결 정합성**: 원본 수집기의 `signal_time_kst` 기준 사후 라벨은 실제 신호 등록 시각(`registered_at_kst`) 이전의 봉을 포함할 수 있으므로, 의사결정 연구에는 반드시 등록 및 신호봉 완료 이후 첫 5분봉 시가(거래량 > 0) 및 왕복 비용을 반영한 재라벨링 결과를 사용해야 합니다.",
        "3. **전진 검증 분리**: 본 표본에서 관찰된 필터 기준(반등형 제외, 시간대 분리 등)은 향후 신규 축적되는 독립 전진 표본(Out-of-Sample)에서 사전 고정된 규칙으로 재검증되어야 합니다.",
    ])

    return "\n".join(lines)


if __name__ == "__main__":
    wt_data_path = (
        Path(__file__).resolve().parents[2]
        / ".worktrees"
        / "t122_panel_refresh"
        / "quant-collector"
        / "data"
        / "resolved_signals.jsonl"
    )
    default_data_path = (
        wt_data_path
        if wt_data_path.exists()
        else Path(__file__).resolve().parents[2] / "quant-collector" / "data" / "resolved_signals.jsonl"
    )
    bars_dir = default_data_path.parent / "bars_history"
    report_output_dir = Path(__file__).resolve().parent / "reports"
    report_output_dir.mkdir(parents=True, exist_ok=True)
    report_output_path = report_output_dir / "collector_signal_analysis_report.md"

    if default_data_path.exists():
        raw_records = CollectorSignalLoader.load_signals(
            default_data_path,
            strategy_version="algorithm260917_v1",
            strict=True,
        )
        if bars_dir.exists():
            eval_records = CollectorSignalLoader.relabel_from_5m_bars(
                raw_records,
                bars_dir,
                one_way_cost_bps=17.5,
            )
        else:
            eval_records = raw_records
        report_text = generate_analysis_report(eval_records, scenario_key="h5_t5_s5")
        report_output_path.write_text(report_text, encoding="utf-8")
        print(f"[OK] Analysis complete: {len(eval_records)} signals processed from {default_data_path}.")
        print(f"[OK] Report saved: {report_output_path}")
    else:
        print(f"[WARN] Data file not found: {default_data_path}")
