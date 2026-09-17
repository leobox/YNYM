# Data Inventory

Source: `D:\DEPO_M\agent\results`
Destination: `quant-research/data/imported`

File counts include nested files. SHA256 tree hashes cover relative path, size, and file bytes.
No `.env`, API key, credential, `.pem`, or `.key` file was selected for this import.

## hourly_pattern

- Files: 222
- Bytes: 5158496
- Tree SHA256: `3043d351e55466dc7ca20eb348bc13535399adf2fdd36cb8972e451444607016`
- Source copy: identical

Sample CSVs:
- `bars_000250.csv`: 354 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume
- `bars_000670.csv`: 354 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume
- `bars_0007C0.csv`: 354 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume

## hourly_pattern_30d

- Files: 11
- Bytes: 1300620
- Tree SHA256: `eb123300810a148b4ded5a74271580b99ceefad15687f69206281f32fdc70681`
- Source copy: identical

Sample CSVs:
- `lag0_above60.csv`: 820 rows; columns: scan, hour, rank, code, name, pattern, score, signal_bar, lag_minutes, volume_ratio, above60, status, hit_1d, hit_3d, hit_5d, return_1d, return_3d, return_5d, hit, entry_price, target_price, hit_bar, sessions_to_hit, return_pct
- `lag0_above60_earlier20.csv`: 526 rows; columns: scan, hour, rank, code, name, pattern, score, signal_bar, lag_minutes, volume_ratio, above60, status, hit_1d, hit_3d, hit_5d, return_1d, return_3d, return_5d, hit, entry_price, target_price, hit_bar, sessions_to_hit, return_pct
- `lag0_baseline.csv`: 826 rows; columns: scan, hour, rank, code, name, pattern, score, signal_bar, lag_minutes, volume_ratio, above60, status, hit_1d, hit_3d, hit_5d, return_1d, return_3d, return_5d, hit, entry_price, target_price, hit_bar, sessions_to_hit, return_pct

## clear_trigger

- Files: 15
- Bytes: 1005556
- Tree SHA256: `50bd5aaaa4ffc57ba91bfd6e05f5cfc95610326f7ba0c99a1a8b3ca894809d3e`
- Source copy: identical

Sample CSVs:
- `lag0_baseline.csv`: 826 rows; columns: scan, day, code, name, signal_bar, score, pattern, volume_component, extension_atr, trend, breakout, money, hold, rank, status, return_pct, hit, entry_price, target_price, hit_bar, sessions_to_hit, exit_bar, stop_price
- `lag0_breakout.csv`: 193 rows; columns: scan, day, code, name, signal_bar, score, pattern, volume_component, extension_atr, trend, breakout, money, hold, rank, status, return_pct, hit, entry_price, target_price, hit_bar, sessions_to_hit, exit_bar, stop_price
- `lag0_hold.csv`: 39 rows; columns: scan, day, code, name, signal_bar, score, pattern, volume_component, extension_atr, trend, breakout, money, hold, rank, hit, entry_price, target_price, hit_bar, sessions_to_hit, return_pct, status, exit_bar, stop_price

## account_hold

- Files: 26
- Bytes: 122153
- Tree SHA256: `3cd006c50d79b0e779190e2db18025ee7c8d1b37d797e1eaa674fa53e1131849`
- Source copy: identical

Sample CSVs:
- `lag0_fixed_0.00175_equity.csv`: 180 rows; columns: time, equity
- `lag0_fixed_0.00175_trades.csv`: 9 rows; columns: code, name, entry, qty, initial_qty, start, day, cost, proceeds, partial, peak, time_exit, last, exit, reason, pnl
- `lag0_fixed_0.0_equity.csv`: 180 rows; columns: time, equity

## ytd_11am

- Files: 204
- Bytes: 13144967
- Tree SHA256: `d76e9cb1e6f2124ae991b8e8c238f9e160c13cd1a29cd92a89ab7888452f1ebc`
- Source copy: identical

Sample CSVs:
- `bars_000250.csv`: 1276 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume
- `bars_000670.csv`: 1276 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume
- `bars_0007C0.csv`: 1091 rows; columns: Unnamed: 0, Open, High, Low, Close, Volume

## pattern_colab

- Files: 19
- Bytes: 237594
- Tree SHA256: `849d1c244a147501dc5fcb28f2f99f0097b040a68535f768fda87a1a994fd4fa`
- Source copy: identical

Sample CSVs:
- `pattern_snapshots/20260915_103733_scores.csv`: 146 rows; columns: 종목, 코드, 구분, 패턴, 점수, RSI, 12/26선, 이격ATR, 3봉회복배수, 동시간배수, 기준봉(KST), _match, scan_time
- `pattern_snapshots/20260915_103733_top5.csv`: 5 rows; columns: 순위, 종목, 코드, 등장, 구분, 패턴, 점수, RSI, 12/26선, 이격ATR, 3봉회복배수, 동시간배수, 기준봉(KST)
- `pattern_snapshots/20260915_103733_universe.csv`: 150 rows; columns: code, name, market, price, market_cap, quote_time, amount, scan_time

## Total

- Files: 497
- Bytes: 20969386
