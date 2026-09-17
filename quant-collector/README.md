# ⏱️ Quant Collector (데이터 자동 수집 & 기록)

GitHub Actions를 활용하여 1시간마다 자동으로 파이썬 스크립트를 실행하고, 수집된 데이터를 기록하는 독립 서브 프로젝트입니다. (자동 매수/주문 기능 배제, 순수 수집 및 관측용)

---

## 📱 모바일 브라우저 활용 팁
1. 스마트폰 모바일 브라우저(Safari, Chrome 등)로 본 저장소의 GitHub 페이지에 접속합니다.
2. 상단 **[Actions]** 탭으로 이동합니다.
3. 좌측 워크플로 목록에서 **"Hourly Quant Data Collector"**를 선택합니다.
4. **[Run workflow]** 버튼을 누르면 원격 서버(GitHub Actions)에서 즉시 `collector.py`가 실행됩니다.
5. 실행 결과 로그 및 생성된 `data/` 로그 파일은 모바일 브라우저에서도 즉시 확인 가능합니다.

---

## ⚙️ 자동 실행 주기 (Cron)
- `.github/workflows/hourly_collector.yml` 설정:
  ```yaml
  schedule:
    - cron: '0 * * * *'  # 매시 정각 1시간 주기 실행
  ```

---

## 📂 파일 구성
- `collector.py`: 실제 데이터 API 호출 및 파일 저장 로직
- `requirements.txt`: 필요한 라이브러리 목록
- `data/`: 수집된 일자별 로그(`jsonl`, `csv` 등) 저장소
