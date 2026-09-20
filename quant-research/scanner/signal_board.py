"""T-052: 신호판(초록/노랑 카드) 마크다운 렌더링과 전진 기록 원장 로직. 네트워크·파일 I/O 없음.

원칙(quant-research/docs/rules.md):
- 점수는 확률이 아니고 돌파선은 추천 매수가가 아니다. '매수 검토' 같은 문구를 쓰지 않는다.
- 최종(🟢) 최대 5개, 0개 허용. 관찰(🟡)을 미달 종목으로 채우지 않는다.
- 데이터 수신 실패를 '신호 없음'으로 위장하지 않는다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TOP_N = 5
DEGRADED_FAIL_RATIO = 0.2   # 수신 실패가 이 비율을 넘으면 '신호 없음'이라고 말하지 않는다
MIN_FORWARD_SAMPLE = 30     # 전진 기록이 이보다 적으면 성과 판단을 하지 않는다

LEDGER_COLS = [
    "signal_date", "code", "name", "market", "close", "pivot_level", "vol_spike", "extension_atr",
    "first_run_id", "status", "entry_date", "entry_price", "exit_date", "exit_price", "net_return_pct",
]
UNRESOLVED = ("PENDING", "OPEN")
# 관찰 구간 성과(같은 결과 판정 규칙으로 채점, 조건을 정한 구간이라 낙관적일 수 있음). 2026-09-20 재검증.
BACKTEST_NOTE = "목표(+10%) 약 35% · 손절(-5%) 약 59% · 5일 만기 약 5%, 비용 뺀 평균 약 +0.25%/건(사실상 본전)"


def format_vol(v) -> str:
    if v is None or pd.isna(v) or v <= 0:
        return "-"
    if v >= 100_000_000:
        return f"약 {v / 100_000_000:.1f}억 주"
    if v >= 10_000:
        return f"약 {v / 10_000:.1f}만 주"
    return f"{int(v):,}주"


def pick_greens(records: list[dict]) -> tuple[list[dict], int]:
    """신호 종목을 거래대금 큰 순·코드 오름차순으로 정렬하고 상위 5개와 잘린 개수를 반환.

    정렬은 우열이 아니라 체결 여건(유동성)을 위한 결정적 순서다.
    """
    ordered = sorted(records, key=lambda r: (-r["turnover"], r["code"]))
    return ordered[:TOP_N], max(0, len(ordered) - TOP_N)


def pick_yellows(records: list[dict]) -> tuple[list[dict], int]:
    """관찰 종목을 돌파선까지 가까운 순·코드 오름차순으로 정렬하고 상위 5개와 잘린 개수를 반환."""
    ordered = sorted(records, key=lambda r: (r["gap_pct"], r["code"]))
    return ordered[:TOP_N], max(0, len(ordered) - TOP_N)


# ---------------------------------------------------------------- 원장

def new_ledger() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in LEDGER_COLS})


def append_signals(ledger: pd.DataFrame, signals: list[dict], run_id: str) -> pd.DataFrame:
    """(signal_date, code) 중복 없이 신규 신호를 원장에 추가한다(재시도·재실행에 멱등)."""
    have = set(zip(ledger["signal_date"].astype(str), ledger["code"].astype(str)))
    rows = []
    for s in signals:
        key = (str(pd.Timestamp(s["signal_date"]).date()), str(s["code"]))
        if key in have:
            continue
        have.add(key)
        rows.append({
            "signal_date": key[0], "code": key[1], "name": s["name"], "market": s["market"],
            "close": s["close"], "pivot_level": s["pivot_level"], "vol_spike": round(float(s["vol_spike"]), 2),
            "extension_atr": round(float(s["extension_atr"]), 2), "first_run_id": run_id,
            "status": "PENDING", "entry_date": "", "entry_price": np.nan, "exit_date": "",
            "exit_price": np.nan, "net_return_pct": np.nan,
        })
    if not rows:
        return ledger
    add = pd.DataFrame(rows, columns=LEDGER_COLS)
    return add if ledger.empty else pd.concat([ledger, add], ignore_index=True)


def unresolved_codes(ledger: pd.DataFrame) -> list[tuple[str, str]]:
    """결과가 아직 확정되지 않은 (code, market) 목록."""
    if ledger.empty:
        return []
    m = ledger[ledger["status"].isin(UNRESOLVED)]
    return sorted(set(zip(m["code"].astype(str), m["market"].astype(str))))


def resolve_ledger(ledger: pd.DataFrame, daily_by_code: dict, resolver) -> pd.DataFrame:
    """미확정 행을 일봉으로 재판정한다. 일봉이 없는 종목은 그대로 둔다(값을 만들지 않는다)."""
    ledger = ledger.copy()
    for i, row in ledger.iterrows():
        if row["status"] not in UNRESOLVED:
            continue
        daily = daily_by_code.get(str(row["code"]))
        if daily is None or daily.empty:
            continue
        out = resolver(daily, pd.Timestamp(row["signal_date"]))
        ledger.at[i, "status"] = out["status"]
        for key in ("entry_date", "exit_date"):
            if key in out:
                ledger.at[i, key] = str(pd.Timestamp(out[key]).date())
        for key in ("entry_price", "exit_price", "net_return_pct"):
            if key in out:
                ledger.at[i, key] = out[key]
    return ledger


def summarize_ledger(ledger: pd.DataFrame) -> dict:
    counts = ledger["status"].value_counts().to_dict() if not ledger.empty else {}
    done = ledger[ledger["status"].isin(["TARGET", "STOP", "TIME"])] if not ledger.empty else ledger
    rets = pd.to_numeric(done["net_return_pct"], errors="coerce").dropna() if not ledger.empty else pd.Series(dtype=float)
    return {
        "total": int(len(ledger)),
        "target": int(counts.get("TARGET", 0)), "stop": int(counts.get("STOP", 0)),
        "time": int(counts.get("TIME", 0)),
        "open": int(counts.get("OPEN", 0) + counts.get("PENDING", 0)),
        "no_fill": int(counts.get("NO_FILL", 0)),
        "resolved": int(len(rets)),
        "mean_net_return_pct": float(rets.mean()) if len(rets) else None,
    }


# ---------------------------------------------------------------- 렌더링

def _won(x) -> str:
    return f"{int(round(float(x))):,}원"


def _green_card(r: dict) -> list[str]:
    return [
        f"#### 🟢 {r['name']} ({r['code']})",
        f"- 마감 **{_won(r['close'])}** (돌파선 {_won(r['pivot_level'])} 위로 마감)",
        f"- ✅ 추세 위 · ✅ 60일 최고가 돌파 · ✅ 거래량 평소의 **{r['vol_spike']:.1f}배** · ✅ 과열 아님",
        "",
    ]


def _yellow_card(r: dict) -> list[str]:
    return [
        f"#### 🟡 {r['name']} ({r['code']})",
        f"- 마감 {_won(r['close'])} → 돌파선 {_won(r['pivot_next'])}까지 **+{r['gap_pct']:.1f}%**",
        f"- 내일 🟢가 되려면: **{_won(r['pivot_next'])} 넘게 마감** + 거래량 **{format_vol(r['req_volume'])} 이상**",
        "",
    ]


def render_board(meta: dict, greens: list[dict], greens_more: int,
                 yellows: list[dict], yellows_more: int, summary: dict) -> str:
    """meta: now_str, last_session, n_total, n_ok, n_fail. 화면 전체(README 삽입 공용)를 만든다."""
    degraded = meta["n_total"] > 0 and meta["n_fail"] / meta["n_total"] > DEGRADED_FAIL_RATIO
    n_green = len(greens) + greens_more
    n_yellow = len(yellows) + yellows_more
    lines = [
        f"> 🕒 **{meta['last_session']} 장 마감 기준** · 스캔 {meta['now_str']} KST · "
        f"{meta['n_ok']}/{meta['n_total']}종목 분석",
        "",
    ]
    if meta.get("n_trend") is not None:
        lines += [f"> 📈 지금 **상승 추세인 종목: {meta['n_trend']}개** (분석한 {meta['n_ok']}개 중). "
                  "적을수록 시장이 약하다는 뜻이고, 그만큼 신호도 드뭅니다.", ""]
    if degraded:
        lines += [
            f"> ⚠️ **데이터 수신이 불안정합니다({meta['n_fail']}종목 실패).** "
            "아래 '없음'을 '신호 없음'으로 읽지 마세요. 다음 스캔을 기다리세요.",
            "",
        ]
    lines += [
        f"## 🚦 신호판: 🟢 {n_green}개 · 🟡 {n_yellow}개",
        "",
        "- 🟢 = 아래 **4가지를 모두** 통과한 종목 (추세 위 · 60일 최고가 돌파 · 거래량 폭발 · 과열 아님)",
        "- 🟡 = 아직은 아니지만 **내일 조건이 맞으면 🟢**가 될 수 있는 종목",
        "- 🟢가 없으면 **쉬는 날**입니다. 억지로 찾지 않습니다.",
        "",
        "### 🟢 신호",
        "",
    ]
    if greens:
        for r in greens:
            lines += _green_card(r)
        if greens_more:
            lines += [f"*외 {greens_more}개는 CSV 스냅샷 참고 (거래대금 큰 순으로 5개만 표시)*", ""]
    else:
        lines += ["*없음*" if degraded else "*오늘은 4가지를 모두 통과한 종목이 없습니다.*", ""]

    lines += ["### 🟡 관찰 (돌파선 4% 이내)", ""]
    if yellows:
        for r in yellows:
            lines += _yellow_card(r)
        if yellows_more:
            lines += [f"*외 {yellows_more}개는 CSV 스냅샷 참고 (돌파선에 가까운 순으로 5개만 표시)*", ""]
    else:
        lines += ["*없음*", ""]

    lines += ["### ⚠️ 꼭 알아두세요", ""]
    lines += [
        "- **검증이 끝나지 않은 연구용 신호**입니다. 사라는 뜻이 아니며, 점수·확률도 아닙니다. 돌파선은 추천 가격이 아닙니다.",
        f"- 과거 시험(조건을 정한 같은 기간이라 실제보다 낙관적): {BACKTEST_NOTE}. "
        "**이기는 횟수보다 지는 횟수가 훨씬 많습니다.**",
    ]
    lines.append(_forward_line(summary))
    lines += ["- 종가·거래량은 야후 파이낸스 일봉(수정 전 가격) 기준입니다. 주문/매수 기능은 없습니다.", ""]
    return "\n".join(lines)


def _forward_line(s: dict) -> str:
    if s["total"] == 0:
        return "- 📒 **실제 기록(전진 검증)**: 아직 기록된 신호가 없습니다. 앞으로 나온 🟢 신호의 결과를 자동으로 쌓습니다."
    base = (f"- 📒 **실제 기록(전진 검증)**: 신호 {s['total']}건 → 목표 {s['target']} · 손절 {s['stop']} · "
            f"5일 만기 {s['time']} · 진행 중 {s['open']}")
    if s["no_fill"]:
        base += f" · 체결불가 {s['no_fill']}"
    if s["resolved"] < MIN_FORWARD_SAMPLE:
        return base + f". 확정 {s['resolved']}건으로 **판단하기엔 아직 너무 적습니다**(최소 {MIN_FORWARD_SAMPLE}건)."
    mean = s["mean_net_return_pct"]
    return base + f". 확정 {s['resolved']}건 비용 뺀 평균 {mean:+.2f}%/건."
