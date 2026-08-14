# 광고 매출 대시보드

Google Sheets(오퍼월 광고 집계)의 벤더별 탭을 GitHub Actions가 주기적으로 읽어와
JSON으로 정리하고, 정적 HTML 페이지들이 이를 읽어 GitHub Pages에서 대시보드로 보여줍니다.
구글 서비스 접속이 막힌 사내망에서도 `*.github.io` 도메인으로 열람하기 위한 구성입니다.

## 화면 구성

| 파일 | 내용 |
| --- | --- |
| `index.html` | 통합 매출 — 5개 벤더(NBT 애디슨, Buzzvil, APCORN SSP, ADOP, Mobwith A)의 일별 매출 추이. 항목별 토글 칩에 선택 기간 합계·데이터 일수 표시, "합계" 선은 켜져 있는 항목만 합산 |
| `mobwith-a.html` | Mobwith A 상세 — 노출수/클릭수/CTR/CPC/정산금액/eCPM의 일별 추이와 기간 집계·표. 기간이 3일 이하면 막대, 그 이상이면 선 그래프 |
| `mobwith-b.html` | Mobwith B 지면별 — 일자×지면 데이터를 히트맵·규모×효율 분포로. 히트맵과 분포도는 각각 독립된 OS(Android/iOS) 필터를 가짐. 지면명 클릭 시 일자별 표, 지표 숫자 클릭 시 그 지표만의 일자별 팝업 그래프(클릭 시에만 그려짐) |

## 동작 방식

1. `.github/workflows/refresh.yml`이 매시 정각(UTC)마다, 또는 Actions 탭에서 수동으로 실행됩니다.
2. `scripts/refresh_data.py`가 스프레드시트의 공개 gviz 엔드포인트로 각 탭을 읽고,
   Frankfurter(ECB 기준) API로 그날그날의 USD→KRW 환율을 받아 APCORN_SSP·ADOP를 원화로 환산합니다.
3. 결과를 `data.json`, `mobwith-a.json`, `mobwith-b.json`에 써서 저장소에 커밋합니다.
4. 각 HTML이 해당 JSON을 `fetch`로 읽어 렌더링하고, 15분마다 자동으로 다시 받아옵니다.
5. Chart.js는 `vendor/chart.umd.min.js`로 한 번만 받아 세 페이지가 공유합니다(외부 CDN
   의존 없음 — 사내망에서 CDN이 막혀도 동작).

## 전제 조건

- 스프레드시트 공유 설정이 **"링크가 있는 모든 사용자: 뷰어"**여야 합니다
  (gviz 엔드포인트는 인증 없이 호출되므로, 링크 공개 상태가 아니면 빈 데이터가 저장됩니다).
- 저장소는 **Public**이어야 합니다 (GitHub Pages 무료 요금제 조건). 즉 매출 수치가
  인터넷에 공개됩니다 — 이 프로젝트를 만들 때 이미 확인하고 진행하기로 한 부분입니다.
- 저장소 설정 → Actions → General → Workflow permissions가
  **"Read and write permissions"**여야 JSON 자동 커밋이 동작합니다.

## 데이터 관련 참고

- `ADPOPCORN_SSP` 탭은 `APCORN_SSP`와 값이 중복되는 레거시 탭으로 판단되어 집계에서 제외했습니다.
- `APCORN_SSP`(media_cost)·`ADOP`(mediaRevNo)는 원본이 USD라 원화로 환산해 표시합니다.
  환율 조회가 실패하면 자동으로 USD 원본 값 표시로 전환됩니다.
- 벤더마다 수집 시작일이 다릅니다(예: Mobwith A는 2026-08-01부터). 통합 화면의 항목 칩에
  선택 기간의 합계와 함께 **실제 데이터가 있는 일수**를 표기하는 이유입니다.
- `Mobwith B`는 2026-08-14부터 지면별 스냅샷이 아니라 **일자×지면 매트릭스**입니다
  (컬럼: 일자·s값(지면 ID)·지면명·OS·노출수·클릭수·CTR·CPC·정산금액·eCPM). 지면명 문자열은
  날짜마다 "상세보기" 표기가 붙거나 빠질 수 있어, s값(지면 ID)을 기준 키로 매칭합니다.
- Mobwith A·B 모두 CTR·CPC·eCPM은 시트 값을 쓰지 않고 노출수·클릭수·정산금액을 선택
  기간만큼 합산한 뒤 직접 계산합니다 — 일별 비율을 단순평균하면 틀린 값이 나오기 때문입니다.
- Mobwith B 히트맵의 색은 값이 아니라 **현재 필터·기간 안에서의 순위(백분위)** 기준입니다.
  지표마다 단위가 달라 값으로는 비교할 수 없기 때문이며, 정렬을 바꿔도 색은 유지됩니다.
- 중복 날짜 행은 자동으로 정리되고, 날짜 공백도 함께 계산해 대시보드에 표시합니다.
