# Clear trigger experiment

Cumulative trend; 6-bar high breakout, close location >=0.7, extension <=4 ATR; estimated hourly turnover >=1bn KRW and >=2x prior same-hour median (20, min5); hold requires preceding trigger and next bar low above breakout level and close >=trigger close. Hold also must meet original eligibility at confirmation. No parameter search. Reused sample, not fresh OOS.

CURRENT COHORT: selection and survivorship bias. Not a historical-universe backtest. Independent overlapping signals, no portfolio return. Gross +5% target, no costs. No parameter tuning. Yahoo venue alignment unverified. Synthetic delay, not recorded feed latency. Session horizons only cover supplied bars, missing final auction may miss targets; mark-to-market uses last supplied close, not official closing price.

Repeated sample; fixed current universe; no fresh out-of-sample evidence. Yahoo 15:00 bar absent. Independent entries, before costs; remaining positions marked to last available close. Earlier20 labels finish within first20 sessions. Baseline targets not retained counts identical stock/time entries, not permanently missed stocks.

|Lag|Rule|Period|N|Target %|Stop %|Mean %|First stock/day N|First stock/day mean %|
|---|---|---|---:|---:|---:|---:|---:|---:|
|0|baseline|all30|687|40.90|44.54|1.98|386|1.98|
|0|baseline|earlier20|400|38.50|48.75|1.45|225|1.83|
|0|baseline|recent10|171|42.69|36.84|2.83|96|2.60|
|0|trend|all30|673|41.01|44.87|1.99|376|1.93|
|0|trend|earlier20|389|37.53|49.36|1.36|214|1.54|
|0|trend|recent10|168|44.64|39.88|2.89|96|2.52|
|0|breakout|all30|155|43.87|41.94|2.51|142|2.05|
|0|breakout|earlier20|91|42.86|38.46|2.79|80|2.19|
|0|breakout|recent10|31|54.84|38.71|3.41|29|2.95|
|0|money|all30|87|51.72|40.23|3.23|80|2.83|
|0|money|earlier20|44|52.27|36.36|3.56|39|3.12|
|0|money|recent10|24|62.50|33.33|4.54|22|4.05|
|0|hold|all30|30|60.00|23.33|5.23|28|4.88|
|0|hold|earlier20|15|80.00|6.67|7.85|13|7.52|
|0|hold|recent10|5|60.00|0.00|7.89|5|7.89|
|60|baseline|all30|679|39.91|46.24|1.74|357|1.62|
|60|baseline|earlier20|392|38.52|49.49|1.36|207|1.45|
|60|baseline|recent10|171|41.52|38.01|2.59|92|2.08|
|60|trend|all30|662|40.33|46.68|1.79|352|1.58|
|60|trend|earlier20|379|37.47|49.87|1.30|202|1.32|
|60|trend|recent10|167|43.71|41.32|2.64|89|2.11|
|60|breakout|all30|164|43.90|40.24|2.58|131|2.50|
|60|breakout|earlier20|92|46.74|34.78|3.11|78|2.77|
|60|breakout|recent10|30|46.67|46.67|2.82|23|3.31|
|60|money|all30|87|51.72|37.93|3.30|70|3.37|
|60|money|earlier20|39|56.41|33.33|3.69|34|3.64|
|60|money|recent10|21|57.14|38.10|4.18|17|4.67|
|60|hold|all30|33|48.48|33.33|3.69|26|4.09|
|60|hold|earlier20|13|76.92|15.38|7.03|11|7.85|
|60|hold|recent10|6|33.33|16.67|3.86|5|4.51|