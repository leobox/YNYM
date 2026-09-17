# 100만원 단일 종목 계좌 비교

Frozen current cohort, reused sample, no fresh OOS; missing 15:00 bars. One position, whole shares, same timestamp highest score then code. No re-entry on exit bar. Partial proceeds stay cash until entire position exits. Hypothetical fee 0.175% each side, no slippage or settlement constraints. Max drawdown measured hourly only. Max 5 sessions exits at available 14:00-bar close (15:00 clock), not 15:30 official close. Last period force liquidation; latest entries included. Partial trailing uses prior completed highs only; activation-bar reversal not modeled. Gap stop may exceed 5%.

|지연(분)|방식|편도비용|최종잔고|수익률|청산거래|수익거래|시간봉 최대낙폭|
|---|---|---|---:|---:|---:|---:|---:|
|0|fixed|0.000%|1,800,056|80.01%|9|8|-8.78%|
|0|fixed|0.175%|1,750,367|75.04%|9|8|-8.92%|
|0|partial|0.000%|1,954,208|95.42%|9|8|-10.56%|
|0|partial|0.175%|1,893,161|89.32%|9|8|-10.84%|
|0|time2|0.000%|1,643,153|64.32%|12|8|-12.72%|
|0|time2|0.175%|1,576,264|57.63%|12|8|-13.37%|
|60|fixed|0.000%|2,079,682|107.97%|11|10|-9.05%|
|60|fixed|0.175%|2,005,310|100.53%|11|10|-9.08%|
|60|partial|0.000%|2,194,780|119.48%|10|9|-8.72%|
|60|partial|0.175%|2,125,711|112.57%|10|9|-9.00%|
|60|time2|0.000%|1,807,687|80.77%|12|8|-13.87%|
|60|time2|0.175%|1,739,117|73.91%|12|8|-14.44%|