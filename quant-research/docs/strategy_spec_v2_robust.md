# Leobox Robust Momentum 60m (LRM-60) — 공식 연구·백테스트 사양서 (v2.0)

* **작성일**: 2026-09-24
* **작성 에이전트**: Antigravity (Gemini)
* **상태**: LRM-60 v2.0 공식 연구·백테스트 사양 (2026-09-24 사용자 승인)
* **검토**: Codex 적대적 검토 및 사용자 승인. 실거래 운용과 전략 성과는 별도 검증 대상.
* **문서 목적**: 기존 v1.0 사양서의 수학적 오류 및 과적합 요소를 해결하고, 60분봉 연구 신호·체결·회계의 재현 가능한 기준을 정의한다.

---

## 1. 개요 및 v1.0 결함 분석 요약 (Review Context)

기존 v1.0 사양서 검토 결과 도출된 핵심 결함과 v2.0에서의 해결 방향입니다:

| 분석 영역 | v1.0 기존 설계의 결함 | v2.0 Robust 개선 설계 |
| :--- | :--- | :--- |
| **자금 관리** | 100만 원 단일 포지션 몰빵은 단일 종목 갭다운에 계좌를 직접 노출 | **3-슬롯 균등 분할 모델(종목당 현재 순자산의 최대 1/3)** 도입. 단, 종목 간 상관·갭·비용 때문에 MDD -10%를 보장하지는 않음 |
| **지표 모델** | 100점 만점 복잡 스코어링 ($S_{\text{vol}}, S_{\text{ATR}}, S_{\text{CLI}}$)의 수식 불일치 및 가중치 과적합 | **3대 핵심 피처 + 4대 All-Pass 게이트**로 단순화. 억지 가중치 제거 |
| **변동성 필터** | $D_{\text{ATR}} \le 4.0$ (종가 기준)은 돌파 후 확장과 베이스 밀착도를 혼합 | **$D_{\text{base}}$ (돌파봉 시가 vs. 직전 봉까지의 SMA26)**로 분리. 단, 3%는 가설 파라미터이며 실측 전에 “역설 해결”로 확정하지 않음 |
| **진입 시점** | 3단계 확인봉 종가 $\ge$ 돌파봉 종가 $\implies$ **돌파선 대비 +5~8% 뜬 최고점(상투) 추격 매수** | 신호봉 마감 후 **익봉($t+1$) 시가 무결 체결 + 갭 상승(+2.5% 초과) 진입 취소** |
| **청산 엔진** | 12봉(1.7거래일) Stagnation 청산으로 우량주 스윙 추세 탑승 불가 및 수수료 누적 | **30개 완료봉 타임아웃**으로 스윙 여유 부여. 4대 청산(Stop, Target, Trailing, Time) 압축 |
| **백테스트 무결성** | 의사코드에서 $t$봉 종가 확인 후 $t$봉 진입(미래참조) 및 장중 터치 누락 | $t$봉 완료 후 $t+1$봉 시가 체결. 진입봉부터 청산을 검사하고, 봉 중에 빈 슬롯과 현금을 같은 봉 시가로 소급 재사용하지 않음 |

---

## 2. 3대 핵심 피처(Core Features) 수학적 정의

지표 간 상호작용과 0분모 예외를 차단하기 위해 3개 지표로 압축합니다.

### A. 상대 거래대금 폭발비 ($R_{\text{vol}}$)
* **목적**: 비대칭 정보 거래자의 진입 확인 (시간대별 왜곡 제거).
* **수식**:
  $$R_{\text{vol}} = \frac{\text{돌파봉 추정 거래대금}}{\max(\operatorname{median}(\text{과거 동일 시간대 최대 20표본의 추정 거래대금}),\, 10^7)}$$
  *(현재 봉은 분모에서 제외하고 최소 5표본을 요구한다. 원천에 실제 거래대금이 없으면 `Close × Volume`을 “추정 거래대금”으로 표기한다.)*
* **통과 조건**: $\text{돌파봉 거래대금} \ge 10\text{억 원}$ AND $R_{\text{vol}} \ge 2.5$

### B. 베이스 이평선 밀착도 ($D_{\text{base}}$) — *VCP 역설 해결*
* **목적**: 장기 이평선(SMA26) 부근에서 수렴을 마친 후 갓 발산하는 초기 자리 포착.
* **수식**:
  $$D_{\text{base}} = \frac{\text{돌파봉 시가} - \text{SMA26}_{t-1}}{\text{SMA26}_{t-1}} \times 100\%$$
* **통과 조건**: $-1.0\% \le D_{\text{base}} \le +3.0\%$

### C. 캔들 종가 장악력 지수 ($\text{CLI}$)
* **목적**: 윗꼬리 매도세를 이겨낸 강한 매수 주도권 확인.
* **수식**:
  $$\text{CLI} = \frac{\text{종가} - \text{저가}}{\text{고가} - \text{저가}}$$
  *(고가=저가인 0분모 봉은 미충족으로 처리한다.)*
* **통과 조건**: $\text{CLI} \ge 0.70$ AND 양봉($\text{종가} > \text{시가}$)

---

## 3. 진입 파이프라인 (Entry Pipeline)

### 3.1 4대 All-Pass 게이트
특정 시점 $t$의 완료된 60분봉이 아래 4개 조건을 모두 충족할 때 유효 신호로 확정합니다:
1. **정배열 추세**: $\text{종가}_t > \text{SMA12}_t > \text{SMA26}_t$
2. **박스권 돌파**: $\text{종가}_t > \max(\text{고가}_{t-20 \dots t-1})$ (직전 20개 봉 박스 상단 돌파)
3. **유동성 폭발**: $\text{거래대금}_t \ge 10\text{억 원}$ AND $R_{\text{vol}} \ge 2.5$
4. **안정적 캔들**: $\text{CLI} \ge 0.70$ AND 양봉 AND $-1.0\% \le D_{\text{base}} \le +3.0\%$

### 3.2 결정론적 우선순위 (Deterministic Selection)
* 빈 슬롯 수보다 통과 종목이 많을 경우:
  $$\text{정렬 기준: } R_{\text{vol}} \text{ 내림차순} \implies \text{종목코드 오름차순}$$

### 3.3 체결 및 과열 방어 (Execution & Overextension Protection)
* 신호 발생 후 **다음 60분봉($t+1$)의 시가(Open)**에 가상 체결.
* **슬리피지**: 편도 $0.15\%$ 가산 체결.
* **과열 갭 상승 방어 룰**: $t+1$봉의 $\text{시가} > \text{신호봉 종가} \times 1.025$ (+2.5% 초과 갭 상승)인 경우 추격 매수를 포기하고 진입 취소.
* **구조 훼손 갭 하락 방어 룰**: 예정 진입가가 `돌파 기준선 × 0.99` 이하이면 진입 즉시 구조적 손절선을 위반하므로 진입 취소.
* 선정된 종목의 $t+1$ 봉이 없거나 거래량 0이면 미체결로 남기고 후순위 종목으로 소급 대체하지 않는다.

### 3.4 파라미터 채택과 민감도 기준

4대 게이트는 서로 완전히 직교하지 않다. 박스 돌파·CLI·$D_{\text{base}}$는 모두 가격 경로에서 파생되고, 추정 거래대금은 주가와 거래량의 곱이다. 따라서 “직교 피처”는 설계 의도일 뿐 실증된 독립성을 뜻하지 않는다.

* 20봉은 공급자가 15:00~15:30 봉을 제공하는지에 따라 약 3거래일이며, 정확한 3거래일 창이 아니다. 문서와 결과에는 “직전 20개 완료봉”으로 표기한다.
* $D_{\text{base}}$의 고정 % 경계는 고변동 소형주에는 과도하게 좁고 저변동 대형주에는 넓을 수 있다. 3%를 견고한 값으로 주장하려면 시가총액·변동성 구간별 통과율과 성과를 같이 보고한다.
* 사전 고정 민감도 그리드는 박스 `12/20/30봉`, $R_{\text{vol}}$ `2.0/2.5/3.0`, 절대 추정 거래대금 `5/10/20억 원`, $D_{\text{base}}$ 상단 `2/3/4%`로 한다. 학습 구간에서 한 점의 최대값을 고르지 말고, 인접 설정에서 신호 수·MDD·비용 후 성과가 함께 유지되는 평탄구간인지 후속 구간으로 확인한다. 통과율과 표본 수를 보기 전에 “헐겁다/희소하다”를 확정하지 않는다.

### 3.5 KRX 15:00~15:30 봉 처리

KRX 정규장은 15:30에 종료되고 15:20~15:30은 종가 단일가 구간이다([KRX 거래시간](https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp), [KRX 종가 단일가 안내](https://global.krx.co.kr/contents/GLB/06/0602/0602010204/GLB0602010204T9.jsp)). 따라서 15:00 봉은 다른 60분 봉과 길이뿐 아니라 체결 방식도 다른 별도 코호트로 다룬다.

* 15:00 봉의 $R_{\text{vol}}$은 직전 서로 다른 시간대 20봉이 아니라, 과거 15:00 봉들의 중앙값과 비교한다. 분당 환산은 상대 폭발비 보정에는 쓸 수 있지만, 실제 체결 용량을 뜻하는 10억 원 절대 게이트를 2배로 부풀리지 않는다.
* 공급자가 15:00~15:30을 제공하지 않거나 14:00 봉 종가를 15:30 공식 종가로 복원할 수 없는 경우, 15시 신호를 제외하고 결측 편향을 보고한다. 현재가 한 번으로 OHLCV를 복원하지 않는다.
* 거래량 0·고가=저가인 공급자 표시용 15:00 봉은 입력에서 제거한다. 실제 무거래 봉 전체를 삭제하는 규칙으로 확대하지 않는다.
* 15:00 신호는 15:30에만 완료되며 $t+1$은 다음 거래일 09:00 봉이다. 오버나잇 갭과 거래정지를 별도 집계하고, 데이터에 실제 다음 봉이 없으면 미체결로 처리한다.
* 단축장·임시 휴장·종목 거래정지는 타임스탬프 간격으로 검증하고, 물리적 다음 행을 무조건 “다음 60분봉”으로 가정하지 않는다.

---

## 4. 실전 4대 청산 엔진 (Exit Engine)

진입 즉시 가동되며, 매 60분봉의 장중(High/Low) 및 시가/종가 기준으로 엄격한 우선순위에 따라 청산합니다.

| 우선순위 | 청산 코드 | 발동 조건 | 체결 가격 및 방식 |
| :---: | :--- | :--- | :--- |
| **1 (Critical)** | **STRUCTURAL_STOP** | 진입가 대비 **-4.0%** 터치 OR 돌파 기준선 대비 **-1.0%** 하향 이탈 | 조건 도달 즉시 시장가 전량 손절 |
| **2 (High)** | **TARGET_HIT** | 진입가 대비 **+8.0%** 도달 시 | 목표가(+8%) 지정가 전량 익절 |
| **3 (Medium)** | **TRAILING_STOP** | 최고 수익률 **+4.0% 이상** 기록 후 고점 대비 **-2.5%** 되돌림 발생 | 이익 보존 시장가 청산 |
| **4 (Low)** | **TIME_EXPIRATION** | 진입봉부터 **30개 완료봉** 보유 후 | 다음 관측 봉 시가 시장가 청산 |

* **불변식 (Tie-Breaking)**: 시가 갭 판정을 먼저 처리한다. 시가가 구조적 손절선 이하면 `STOP_GAP`, 목표가 이상이면 `TARGET_GAP`, 활성화된 트레일링선 이하이면 `TRAILING_GAP`이다. 갭 청산은 해당 봉 **시가**에 불리한 슬리피지를 적용한다. 그 후 한 봉의 High/Low가 Target과 구조적 Stop 또는 활성화된 Trailing Stop을 모두 접촉하면 **Stop 우선**으로 처리한다.
* **트레일링 봉 내 순서**: OHLC만으로는 고가와 저가의 선후를 알 수 없으므로, 이전 완료봉까지 형성된 고점으로만 현재 봉의 트레일링 손절을 활성화한다. 현재 봉의 신고가는 다음 봉부터 반영한다.
* **타임아웃 계수**: 진입봉을 첫 번째 노출 봉으로 계수한다. 30개 완료봉 보유 후 다음 관측 봉 시가에 청산한다. `30봉=5거래일`은 공급자의 봉 분할에 따라 성립하지 않으므로 성과 보고에서는 봉 수와 거래일 수를 별도 표시한다.

---

## 5. 포지션 사이징 및 리스크 한도

* **총자산 운용 슬롯**: 최대 **3개 포지션**, 신규 진입의 매수 비용은 진입 시점 순자산의 $1/3$을 초과하지 않는다.
* **1회 거래 명목 리스크 (Nominal Risk per Trade, 갭·비용 제외)**:
  $$\text{계좌 리스크} = 33.3\% \times 4.0\% = \mathbf{1.33\%}$$
* **제한된 시나리오 예시**: 갭·슬리피지·비용이 없고 매번 정확히 순자산의 1/3만 진입하여 -4%에 손절된다면, 5회 손절의 복리 손실은 $1-(1-0.0133)^5\approx6.5\%$이다.
* 위 계산은 **MDD 보장이 아니다**. 상관된 3종목의 동시 하락, 손절선을 넘는 갭, 거래정지, 슬리피지·비용, 8회 이상의 손실 군집은 -10%를 넘을 수 있다. 승률만으로 MDD를 증명할 수 없으며, 시간봉 mark-to-market 자산 곡선으로 실측한다.

---

## 6. Python 백테스트 레퍼런스 코드

입력은 Asia/Seoul 기준으로 오름차순·중복 없는 완료봉이어야 하며, `Open`은 실제 첫 체결가여야 한다. 수정주가 OHLC와 원거래량을 섞어 거래대금을 산출하지 않는다. `step(ts, next_ts)`는 `ts` 신호를 확정한 후 역사적 `next_ts` 봉이 완료됐을 때 호출하는 순차 백테스트용이다. `next_ts` 최종 거래량은 0거래 봉의 미체결 확인에만 쓰고 신호 선정이나 후순위 대체에 사용하지 않는다. 보유 종목의 무거래 봉에서도 청산 체결을 만들지 않고, 평가액은 미정의로 기록한다.

기간 종료 봉은 실험 전에 고정하고 그 봉의 시가에서 `finalize_at_open(ts)`를 호출한다. 같은 봉에 `step`을 다시 호출하지 않는다. 종료 봉이 없거나 거래량 0이면 강제 체결을 꾸며내지 않고 미청산 포지션과 미정의 계좌 수익을 보고한다.

```python
import pandas as pd
import numpy as np

class RobustQuantEngine:
    def __init__(self, data_feed: dict[str, pd.DataFrame], initial_equity=1_000_000):
        self.data = data_feed
        self.cash = initial_equity
        self.equity = initial_equity
        self.positions = {}
        self.max_slots = 3
        self.fee_side = 0.0023 / 2  # 왕복 비용 0.23% 단순 가정의 편도분
        self.slippage = 0.0015      # 시장가성 체결의 편도 불리한 슬리피지
        self.trades = []
        self.data_gaps = []

        required = {'Open', 'High', 'Low', 'Close', 'Volume'}
        for code, df in self.data.items():
            if not required.issubset(df.columns):
                raise ValueError(f"{code}: missing OHLCV columns")
            if not isinstance(df.index, pd.DatetimeIndex) or str(df.index.tz) != 'Asia/Seoul':
                raise ValueError(f"{code}: index timezone must be Asia/Seoul")
            if not df.index.is_monotonic_increasing or not df.index.is_unique:
                raise ValueError(f"{code}: index must be sorted and unique")
            ohlcv = df[['Open', 'High', 'Low', 'Close', 'Volume']].to_numpy(dtype=float)
            if not np.isfinite(ohlcv).all() or (df['Volume'] < 0).any():
                raise ValueError(f"{code}: OHLCV must be finite and volume non-negative")
            if ((df['High'] < df[['Open', 'Low', 'Close']].max(axis=1)) |
                    (df['Low'] > df[['Open', 'High', 'Close']].min(axis=1))).any():
                raise ValueError(f"{code}: invalid OHLC relationship")
            display_bar = ((df.index.hour == 15) & (df['Volume'] == 0) &
                           (df['High'] == df['Low']))
            if display_bar.any():
                raise ValueError(f"{code}: remove 15:00 zero-volume display bars")

    def evaluate_signals(self, ts, excluded=frozenset()):
        candidates = []
        for code, df in self.data.items():
            if code in excluded or ts not in df.index:
                continue
            idx = df.index.get_loc(ts)
            if idx < 26:
                continue

            bar = df.iloc[idx]
            hist = df.iloc[idx-20:idx]
            values = bar[['Open', 'High', 'Low', 'Close', 'Volume']].to_numpy(dtype=float)
            if not np.isfinite(values).all() or bar['Volume'] < 0 or bar['High'] < bar['Low']:
                continue

            sma12 = df['Close'].iloc[idx-11:idx+1].mean()
            sma26 = df['Close'].iloc[idx-25:idx+1].mean()
            sma26_prev = df['Close'].iloc[idx-26:idx].mean()
            high_prev20 = hist['High'].max()

            previous = df.iloc[:idx]
            same_slot = previous[previous.index.time == ts.time()]
            amount_hist = (same_slot['Close'] * same_slot['Volume']).tail(20)
            if len(amount_hist) < 5:
                continue

            amount = bar['Close'] * bar['Volume']
            r_vol = amount / max(float(amount_hist.median()), 1e7)
            if not np.isfinite([sma12, sma26, sma26_prev, high_prev20, r_vol]).all() or sma26_prev <= 0:
                continue

            d_base = (bar['Open'] - sma26_prev) / sma26_prev * 100.0
            candle_range = bar['High'] - bar['Low']
            if candle_range <= 0:
                continue
            cli = (bar['Close'] - bar['Low']) / candle_range

            # 4대 게이트
            is_trend = bar['Close'] > sma12 > sma26
            is_breakout = bar['Close'] > high_prev20
            is_volume = (amount >= 1e9) and (r_vol >= 2.5)
            is_candle = (cli >= 0.70) and (bar['Close'] > bar['Open']) and (-1.0 <= d_base <= 3.0)

            if is_trend and is_breakout and is_volume and is_candle:
                candidates.append({
                    'code': code,
                    'breakout_level': high_prev20,
                    'signal_close': bar['Close'],
                    'r_vol': r_vol
                })

        return sorted(candidates, key=lambda x: (-x['r_vol'], x['code']))

    def _bar(self, code, ts):
        df = self.data[code]
        return None if ts not in df.index else df.loc[ts]

    def _sell(self, code, price, reason, ts):
        pos = self.positions.pop(code)
        proceeds = pos['qty'] * price * (1 - self.fee_side)
        self.cash += proceeds
        self.trades.append({
            **pos, 'exit_ts': ts, 'exit_price': price, 'reason': reason,
            'pnl': proceeds - pos['cost']
        })

    def step(self, ts, next_ts):
        # 봉 시가에 알 수 있는 슬롯·현금만 진입에 쓴다.
        starting_codes = set(self.positions)
        missing_held = {
            code for code in starting_codes
            if (self._bar(code, next_ts) is None or
                self._bar(code, next_ts)['Volume'] <= 0)
        }
        if missing_held:
            self.data_gaps.append({'ts': next_ts, 'codes': sorted(missing_held)})
        entry_slots = 0 if missing_held else self.max_slots - len(starting_codes)
        selected = self.evaluate_signals(ts, excluded=starting_codes)[:entry_slots]
        entry_cash_left = self.cash

        equity_at_open = self.cash
        for code, pos in self.positions.items():
            bar = self._bar(code, next_ts)
            mark = pos['last_close'] if bar is None else float(bar['Open'])
            equity_at_open += pos['qty'] * mark
        slot_cap = equity_at_open / self.max_slots

        # 1. 기존 포지션의 시가 갭·타임아웃. 여기서 생긴 슬롯과 현금은 이 봉에 재사용하지 않는다.
        exited = set()
        for code in starting_codes:
            bar = self._bar(code, next_ts)
            if bar is None or bar['Volume'] <= 0:
                continue
            pos = self.positions[code]
            stop_p = max(pos['entry_price'] * 0.96, pos['breakout_level'] * 0.99)
            target_p = pos['entry_price'] * 1.08
            trailing_p = (pos['peak_price'] * 0.975
                          if pos['peak_price'] >= pos['entry_price'] * 1.04 else None)
            if bar['Open'] <= stop_p:
                self._sell(code, bar['Open'] * (1 - self.slippage), "STOP_GAP", next_ts)
                exited.add(code)
            elif bar['Open'] >= target_p:
                self._sell(code, bar['Open'] * (1 - self.slippage), "TARGET_GAP", next_ts)
                exited.add(code)
            elif trailing_p is not None and bar['Open'] <= trailing_p:
                self._sell(code, bar['Open'] * (1 - self.slippage), "TRAILING_GAP", next_ts)
                exited.add(code)
            elif pos['bars_held'] >= 30:
                self._sell(code, bar['Open'] * (1 - self.slippage), "TIME_EXPIRATION", next_ts)
                exited.add(code)

        # 2. 신규 진입. 선정 후 미체결/취소된 종목을 후순위로 대체하지 않는다.
        for sig in selected:
            bar = self._bar(sig['code'], next_ts)
            if bar is None or bar['Volume'] <= 0 or not np.isfinite(bar['Open']):
                continue
            if bar['Open'] > sig['signal_close'] * 1.025:
                continue

            entry_price = bar['Open'] * (1 + self.slippage)
            if entry_price <= sig['breakout_level'] * 0.99:
                continue
            budget = min(slot_cap, entry_cash_left)
            qty = int(budget // (entry_price * (1 + self.fee_side)))
            if qty <= 0:
                continue

            cost = qty * entry_price * (1 + self.fee_side)
            self.cash -= cost
            entry_cash_left -= cost
            self.positions[sig['code']] = {
                'code': sig['code'], 'entry_ts': next_ts,
                'entry_price': entry_price, 'qty': qty, 'cost': cost,
                'breakout_level': sig['breakout_level'],
                'peak_price': entry_price, 'last_close': entry_price,
                'bars_held': 0
            }

        # 3. 기존+신규 포지션의 장중 터치. 이전 봉 고점만 트레일링에 사용한다.
        for code in list(self.positions):
            if code in exited:
                continue
            bar = self._bar(code, next_ts)
            if bar is None or bar['Volume'] <= 0:
                continue
            pos = self.positions[code]
            stop_p = max(pos['entry_price'] * 0.96, pos['breakout_level'] * 0.99)
            target_p = pos['entry_price'] * 1.08
            trailing_p = (pos['peak_price'] * 0.975
                          if pos['peak_price'] >= pos['entry_price'] * 1.04 else None)

            if bar['Low'] <= stop_p:  # 구조적 Stop/Target 동시 접촉은 Stop 우선
                self._sell(code, stop_p * (1 - self.slippage), "STOP_TOUCH", next_ts)
            elif trailing_p is not None and bar['Low'] <= trailing_p:
                self._sell(code, trailing_p * (1 - self.slippage), "TRAILING_STOP", next_ts)
            elif bar['High'] >= target_p:
                self._sell(code, target_p, "TARGET_TOUCH", next_ts)
            else:
                pos['peak_price'] = max(pos['peak_price'], float(bar['High']))
                pos['last_close'] = float(bar['Close'])
                pos['bars_held'] += 1

        missing_marks = {
            code for code in self.positions
            if (self._bar(code, next_ts) is None or
                self._bar(code, next_ts)['Volume'] <= 0)
        }
        self.equity = (np.nan if missing_marks else self.cash + sum(
            pos['qty'] * pos['last_close'] for pos in self.positions.values()
        ))

    def finalize_at_open(self, ts):
        # 사전 고정한 종료 봉에서만 호출하고, 같은 봉의 step은 실행하지 않는다.
        unresolved = []
        for code in list(self.positions):
            bar = self._bar(code, ts)
            if bar is None or bar['Volume'] <= 0:
                unresolved.append(code)
                continue
            self._sell(code, bar['Open'] * (1 - self.slippage), "PERIOD_END", ts)
        if unresolved:
            self.data_gaps.append({'ts': ts, 'codes': sorted(unresolved)})
        self.equity = self.cash if not self.positions else np.nan
        return unresolved
```

---

## 7. 적대적 검토 반영 결론과 채택 게이트

1. **파라미터**: 20봉·2.5배·10억 원·$D_{\text{base}}$ 3%는 현재 가설이다. 신호 수와 사후 성과를 실측하지 않은 상태에서 적절하거나 견고하다고 판정하지 않는다. §3.4의 사전 고정 그리드와 후속 구간을 통과해야 채택한다.
2. **청산 순서**: 시가 갭을 먼저 판정하고, 트레일링선 아래 갭도 시가에 청산한다. 같은 봉의 구조적·트레일링 Stop과 Target 동시 접촉은 Stop 우선으로 한다. 트레일링은 이전 봉까지의 고점만 쓰며, 진입봉을 포함해 청산 게이트를 검사한다.
3. **15:00 봉**: 같은 15:00 코호트와 비교하되 10억 원 절대 유동성은 실제 금액을 유지한다. 공급자가 마지막 30분을 누락하면 15시 신호를 만들지 않는다.
4. **계좌 불변식**: 봉 시작 시 점유 중인 슬롯은 그 봉의 장중 청산으로 신규 진입에 쓸 수 없다. 청산대금도 같은 봉 시가로 소급 재사용하지 않는다. 매수·매도 비용은 왕복 0.23% 가정을 편도로 나누어 한 번씩만 부과한다. 사전 고정한 기간 종료 봉의 시가에서 잔여 포지션을 청산하며 미체결은 별도 보고한다.

본 문서는 **LRM-60 v2.0의 공식 연구·백테스트 사양**이다. 게이트 수치와 수익성은 아직 실증되지 않은 연구 가설이며 운영 전략으로 채택되지 않았다. 실제 시장 원본을 이용한 prefix 불변성, 갭·미체결·30봉 만기·기간 말 잔여 포지션 처리·현금/자산 보존과 고정된 후속 구간 검증을 통과하기 전에는 “Robust”를 성과 주장으로 사용하지 않는다.
