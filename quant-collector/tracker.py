"""
Signal Tracker & Forward Labeler for Quant Collector
---------------------------------------------------
알고리즘(algorithm260917)이 포착한 신호의 특징값을 고정(Snapshot)하고,
향후 3~5거래일 동안 완료봉을 추적하여 다중 목표/손절 기준에 따른 선접촉 라벨을 확정합니다.

[라벨 정의 (다중 상태)]
- TARGET_FIRST: 손절선보다 목표가를 먼저 접촉
- STOP_FIRST  : 목표가보다 손절선을 먼저 접촉
- AMBIGUOUS   : 동일 시간봉 내에서 목표가와 손절가를 모두 접촉 (보수적 손절 간주 가능)
- TIMEOUT     : 보유 기간(3일 또는 5일) 동안 둘 다 미접촉
- NO_FILL     : 진입 시점 거래량 0 또는 데이터 결측

[다중 목표/손절 매트릭스]
- 목표: +3%, +5%, +7%, +10%
- 손절: -3%, -5%
- 기간: 3거래일, 5거래일
"""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Set
import pandas as pd

KST = timezone(timedelta(hours=9))
TARGET_PCTS = [3.0, 5.0, 5.35, 7.0, 10.0]
STOP_PCTS = [3.0, 5.0]
HORIZONS = [3, 5]  # 거래일 기준


def eval_key(h: int, t_pct: float, s_pct: float) -> str:
    t_str = "535" if t_pct == 5.35 else str(int(t_pct))
    return f"h{h}_t{t_str}_s{int(s_pct)}"


class SignalTracker:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.pending_file = data_dir / "pending_signals.json"
        self.resolved_file = data_dir / "resolved_signals.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_signals = self._load_pending()

    def _load_pending(self) -> Dict[str, Any]:
        if self.pending_file.exists():
            try:
                with open(self.pending_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[경고] pending_signals.json 로드 실패: {e}")
                return {}
        return {}

    def _save_pending(self):
        with open(self.pending_file, "w", encoding="utf-8") as f:
            json.dump(self.pending_signals, f, ensure_ascii=False, indent=2)

    def _append_resolved(self, resolved_record: Dict[str, Any]):
        with open(self.resolved_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(resolved_record, ensure_ascii=False) + "\n")

    def get_tracked_codes(self) -> Set[str]:
        """현재 추적 중인 모든 종목 코드 반환 (유니버스 150위 밖으로 밀려나도 계속 수집)"""
        return {sig["code"] for sig in self.pending_signals.values()}

    def register_signal(
        self,
        strategy_version: str,
        code: str,
        name: str,
        market: str,
        bar_time_kst: str,
        features: Dict[str, Any],
        entry_reference_price: float,
    ) -> Optional[str]:
        """신규 신호 등록 (중복 방지 멱등성 보장, 종목당 활성 신호 1건 제한)"""
        sig_id = f"{strategy_version}_{code}_{bar_time_kst.replace(' ', '_').replace(':', '')}"
        if sig_id in self.pending_signals:
            return None  # 이미 등록되어 추적 중

        # 같은 전략에서 같은 종목이 아직 미해소 상태로 추적 중이면 재등록하지 않는다.
        # (연속봉마다 조건을 계속 만족하는 종목이 봉마다 별도 신호로 중복 등록되는 것을 방지)
        for sig in self.pending_signals.values():
            if sig["strategy_version"] == strategy_version and sig["code"] == code and not sig["is_fully_resolved"]:
                return None

        now_dt = datetime.now(KST)

        # 목표/손절 매트릭스 사전 계산
        targets = {f"tgt_{int(p)}": round(entry_reference_price * (1 + p / 100.0), 2) for p in TARGET_PCTS if p != 5.35}
        targets["tgt_5_35"] = round(entry_reference_price * 1.0535, 2)
        stops = {f"stop_{int(p)}": round(entry_reference_price * (1 - p / 100.0), 2) for p in STOP_PCTS}

        # 라벨 판정 상태 초기화 (조합별: (tgt, stop, horizon))
        evaluations = {}
        for h in HORIZONS:
            for t_pct in TARGET_PCTS:
                for s_pct in STOP_PCTS:
                    key = eval_key(h, t_pct, s_pct)
                    evaluations[key] = {
                        "status": "PENDING",
                        "exit_price": None,
                        "exit_time": None,
                        "return_pct": None,
                    }

        signal_obj = {
            "signal_id": sig_id,
            "strategy_version": strategy_version,
            "code": code,
            "name": name,
            "market": market,
            "signal_time_kst": bar_time_kst,
            "registered_at_kst": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_reference_price": entry_reference_price,
            "targets": targets,
            "stops": stops,
            "features": features,
            "trading_days_observed": 0,
            "observed_dates": [],
            "highest_seen": entry_reference_price,
            "lowest_seen": entry_reference_price,
            "evaluations": evaluations,
            "is_fully_resolved": False,
        }

        self.pending_signals[sig_id] = signal_obj
        self._save_pending()
        return sig_id

    def update_with_bars(self, code_candles: Dict[str, List[Dict[str, Any]]], now_dt: datetime) -> List[Dict[str, Any]]:
        """
        수집된 최신 완료봉들을 바탕으로 pending_signals의 가격 접촉 및 만기 추적
        반환: 이번 업데이트에서 완전 확정(Resolved)되어 아카이브된 신호 목록
        """
        newly_resolved = []
        still_pending = {}

        for sig_id, sig in self.pending_signals.items():
            code = sig["code"]
            candles = code_candles.get(code, [])
            if not candles:
                still_pending[sig_id] = sig
                continue

            entry_p = sig["entry_reference_price"]
            sig_time = sig["signal_time_kst"]
            if len(sig_time) <= 11:
                sig_time = f"{now_dt.year}-{sig_time}"

            # 신호 발생 시각 이후의 완료봉들만 필터링 (미래봉)
            future_bars = [c for c in candles if c["time_kst"] > sig_time]
            if not future_bars:
                still_pending[sig_id] = sig
                continue

            # 관측 일자 집합 갱신
            observed_dates = sorted(set(b["time_kst"].split(" ")[0] for b in future_bars))
            sig["observed_dates"] = observed_dates
            sig["trading_days_observed"] = len(observed_dates)

            # 전 기간 최고가/최저가 갱신
            for b in future_bars:
                sig["highest_seen"] = max(sig["highest_seen"], b["high"])
                sig["lowest_seen"] = min(sig["lowest_seen"], b["low"])

            # 모든 매트릭스 조합별 라벨 판정
            all_resolved = True

            for h in HORIZONS:
                for t_pct in TARGET_PCTS:
                    for s_pct in STOP_PCTS:
                        key = eval_key(h, t_pct, s_pct)
                        if key not in sig["evaluations"]:
                            sig["evaluations"][key] = {
                                "status": "PENDING",
                                "exit_price": None,
                                "exit_time": None,
                                "return_pct": None,
                            }
                        eval_state = sig["evaluations"][key]

                        if eval_state["status"] != "PENDING":
                            continue  # 이미 확정된 조합

                        target_p = entry_p * (1 + t_pct / 100.0)
                        stop_p = entry_p * (1 - s_pct / 100.0)

                        # 바 순차 탐색
                        status = "PENDING"
                        exit_price = None
                        exit_time = None
                        ret_pct = None

                        for b_idx, bar in enumerate(future_bars):
                            bar_date = bar["time_kst"].split(" ")[0]
                            day_rank = observed_dates.index(bar_date) + 1  # 1-indexed

                            # 기간(h 거래일) 초과 여부
                            if day_rank > h:
                                # 기간 만료 시 직전 종가로 청산
                                prev_bar = future_bars[b_idx - 1] if b_idx > 0 else bar
                                status = "TIMEOUT"
                                exit_price = prev_bar["close"]
                                exit_time = prev_bar["time_kst"]
                                ret_pct = round((exit_price / entry_p - 1) * 100.0, 2)
                                break

                            # 봉 내 접촉 판정
                            b_open = bar["open"]
                            b_high = bar["high"]
                            b_low = bar["low"]

                            hit_stop = b_low <= stop_p or b_open <= stop_p
                            hit_target = b_high >= target_p or b_open >= target_p

                            # 1. 갭상 / 갭하
                            if b_open <= stop_p:
                                status = "STOP_FIRST"
                                exit_price = b_open
                                exit_time = bar["time_kst"]
                                ret_pct = round((exit_price / entry_p - 1) * 100.0, 2)
                                break
                            elif b_open >= target_p:
                                status = "TARGET_FIRST"
                                exit_price = target_p  # 지정가 체결 가정
                                exit_time = bar["time_kst"]
                                ret_pct = t_pct
                                break

                            # 2. 동일 봉 동시 접촉 -> 보수적 STOP_FIRST (AMBIGUOUS 플래그)
                            if hit_stop and hit_target:
                                status = "AMBIGUOUS_STOP"
                                exit_price = stop_p
                                exit_time = bar["time_kst"]
                                ret_pct = -s_pct
                                break

                            # 3. 단독 접촉
                            if hit_stop:
                                status = "STOP_FIRST"
                                exit_price = stop_p
                                exit_time = bar["time_kst"]
                                ret_pct = -s_pct
                                break
                            elif hit_target:
                                status = "TARGET_FIRST"
                                exit_price = target_p
                                exit_time = bar["time_kst"]
                                ret_pct = t_pct
                                break

                        # 5일 전체 관찰 후에도 미접촉이고 만기 도달한 경우
                        if status == "PENDING" and len(observed_dates) >= h:
                            status = "TIMEOUT"
                            last_bar = future_bars[-1]
                            exit_price = last_bar["close"]
                            exit_time = last_bar["time_kst"]
                            ret_pct = round((exit_price / entry_p - 1) * 100.0, 2)

                        eval_state["status"] = status
                        eval_state["exit_price"] = exit_price
                        eval_state["exit_time"] = exit_time
                        eval_state["return_pct"] = ret_pct

                        if status == "PENDING":
                            all_resolved = False

            # 모든 horizon 조합이 만료되었거나 종결되었는지 확인 (최대 h=5거래일 경과 시 완전 종결)
            if all_resolved or len(observed_dates) >= max(HORIZONS):
                sig["is_fully_resolved"] = True
                sig["resolved_at_kst"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                self._append_resolved(sig)
                newly_resolved.append(sig)
            else:
                still_pending[sig_id] = sig

        self.pending_signals = still_pending
        self._save_pending()
        return newly_resolved

    def get_summary_stats(self, strategy_version: Optional[str] = None, ref_key: str = "h5_t535_s5") -> Dict[str, Any]:
        """현재까지 추적 중인 신호 및 완료된 신호의 통계 요약

        strategy_version을 주면 그 전략으로 등록된 신호만 집계한다(운영 신호와
        병렬 실험 전략의 성과를 섞지 않기 위함).

        ref_key: 어떤 목표/손절/기간 조합을 익절·손절 판정 기준으로 쓸지
        (evaluations 딕셔너리의 키, 기본값: "h5_t535_s5" = +5.35% 익절/-5% 손절/5거래일).
        """
        total_pending = sum(
            1 for sig in self.pending_signals.values()
            if strategy_version is None or sig.get("strategy_version") == strategy_version
        )
        resolved_count = 0
        target_first_count = 0
        stop_first_count = 0
        timeout_count = 0

        if self.resolved_file.exists():
            with open(self.resolved_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        if strategy_version is not None and record.get("strategy_version") != strategy_version:
                            continue
                        resolved_count += 1
                        evals = record.get("evaluations", {})
                        st = evals.get(ref_key, {}).get("status")
                        if st is None and ref_key == "h5_t535_s5":
                            st = evals.get("h5_t5_s5", {}).get("status") or evals.get("h5_t10_s5", {}).get("status")
                        if st == "TARGET_FIRST":
                            target_first_count += 1
                        elif st in ("STOP_FIRST", "AMBIGUOUS_STOP"):
                            stop_first_count += 1
                        elif st == "TIMEOUT":
                            timeout_count += 1
                    except Exception:
                        pass

        return {
            "total_pending": total_pending,
            "total_resolved": resolved_count,
            "ref_benchmark": ref_key,
            "target_first": target_first_count,
            "stop_first": stop_first_count,
            "timeout": timeout_count,
            "win_rate": round(target_first_count / resolved_count * 100.0, 1) if resolved_count > 0 else None,
        }
