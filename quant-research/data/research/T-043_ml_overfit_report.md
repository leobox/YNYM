# T-043 ML 과적합(Overfitting) vs 일반화(Generalization) 백테스트 보고서

| Model                                       |   Train_Trades |   Train_WinRate_% |   Train_Return_% |   Test_Trades |   Test_WinRate_% |   Test_Return_% |   Test_PF |   Test_MDD_% |
|:--------------------------------------------|---------------:|------------------:|-----------------:|--------------:|-----------------:|----------------:|----------:|-------------:|
| 1. Extreme Overfit Tree (과적합 끝판왕)     |              3 |               100 |            23.9  |             5 |             20   |           -7.69 |      0.39 |        12.62 |
| 2. Overfit Random Forest (복잡도 무제한 RF) |              3 |               100 |            25.98 |             9 |             33.3 |          -18.14 |      0.35 |        21.76 |
| 3. Regularized Boost (규제 적용 HGB)        |              5 |                60 |            13.85 |             9 |             44.4 |           -7.34 |      0.68 |        16.76 |
