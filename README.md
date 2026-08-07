# 오퍼월 광고 매출 대시보드

Google Sheets(오퍼월 광고 집계)의 4개 벤더 탭(NBT_Adison, Buzzvil, APCORN_SSP, ADOP)을
GitHub Actions가 주기적으로 읽어와 `data.json`으로 정리하고, `index.html`이 이를
읽어 GitHub Pages에서 대시보드로 보여줍니다. 구글 서비스 접속이 막힌 사내망에서도
`*.github.io` 도메인으로는 열람할 수 있도록 하기 위한 구성입니다.

## 동작 방식

1. `.github/workflows/refresh.yml`이 매시 정각(UTC)마다, 또는 Actions 탭에서 수동으로 실행됩니다.
2. `scripts/refresh_data.py`가 스프레드시트의 공개 gviz 엔드포인트로 4개 탭을 읽고,
   Frankfurter(ECB 기준) API로 그날그날의 USD→KRW 환율을 받아 APCORN_SSP·ADOP를 원화로 환산합니다.
3. 결과를 `data.json`에 써서 저장소에 커밋합니다.
4. `index.html`은 `fetch('./data.json')`으로 이 값을 읽어 렌더링하고, 15분마다 자동으로 다시 받아옵니다.

## 전제 조건

- 스프레드시트 공유 설정이 **"링크가 있는 모든 사용자: 뷰어"**로 되어 있어야 합니다
  (gviz 엔드포인트는 인증 없이 호출되므로, 링크 공개 상태가 아니면 빈 데이터가 저장됩니다).
- 저장소는 **Public**이어야 합니다 (GitHub Pages 무료 요금제 조건). 즉 매출 수치가
  인터넷에 공개됩니다 — 이 프로젝트를 만들 때 이미 확인하고 진행하기로 한 부분입니다.
- 저장소 설정 → Actions → General → Workflow permissions가
  **"Read and write permissions"**로 되어 있어야 `data.json` 자동 커밋이 동작합니다.

## 데이터 관련 참고

- `ADPOPCORN_SSP` 탭은 `APCORN_SSP`와 값이 중복되는 레거시 탭으로 판단되어 집계에서 제외했습니다.
- `APCORN_SSP`(media_cost)·`ADOP`(mediaRevNo)는 원본이 USD라 원화로 환산해 표시합니다.
  환율 조회가 실패하면 자동으로 USD 원본 값 표시로 전환됩니다.
- 중복 날짜 행은 자동으로 정리되고, 벤더 전체에 걸친 날짜 공백도 함께 계산해 대시보드에 표시합니다.
