# T-043 ML 과적합(Overfitting) vs 일반화(Generalization) 백테스트 보고서

| Model                                       |   Train_Trades |   Train_WinRate_% |   Train_Return_% |   Test_Trades |   Test_WinRate_% |   Test_Return_% |   Test_PF |   Test_MDD_% |
|:--------------------------------------------|---------------:|------------------:|-----------------:|--------------:|-----------------:|----------------:|----------:|-------------:|
| 1. Extreme Overfit Tree (과적합 끝판왕)     |              3 |               100 |            23.73 |             5 |             20   |           -7.84 |      0.39 |        15.58 |
| 2. Overfit Random Forest (복잡도 무제한 RF) |              3 |               100 |            25.8  |             7 |             42.9 |          -10.74 |      0.47 |        20.12 |
| 3. Regularized Boost (규제 적용 HGB)        |              5 |                60 |            13.61 |             6 |             50   |           -5.9  |      0.49 |        16.24 |
