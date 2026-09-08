# 유니버스 RS 대시보드

코스피200·코스닥150(시가총액 상위) + 미국 S&P500 + 주요 ETF의 상대강도(RS)를 계산해서
**포트폴리오 두 개**(자동/수동)의 업종별 비중을 보여주는 대시보드입니다.
**전부 무료**(GitHub + FinanceDataReader, 선택적으로 토스증권 Open API)로 돌아갑니다.

## 포트폴리오는 두 개입니다

- **자동** — 사람이 손으로 넣는 값 없이, RS(상대강도) 계산 결과로 비중을 자동 산출합니다.
  `data/portfolio.json`.
- **수동** — 본인이 실제로 들고 있는(또는 원하는) 종목·비중을 직접 적어 넣는 탭입니다.
  `data/manual.json`을 열어 종목코드와 비중만 적으면 되고, 처음엔 비어 있습니다.
  `data/manual_portfolio.json`이 화면에 실제로 표시되는 결과이고, Actions가 자동 생성합니다
  (`data/manual.json`은 사람이 입력하는 파일이라 Actions가 절대 덮어쓰지 않습니다).

대시보드 상단의 **포트폴리오: 자동/수동** 버튼으로 전환해서 봅니다.

상단 탭의 **52주 신고가·신저가**에서는 유니버스 전체(코스피200+코스닥150+S&P500+ETF) 중
오늘 종가가 최근 252거래일(≈52주) 동안의 최고가/최저가와 같은 종목을 시장별로 필터링해서
볼 수 있습니다 — RS와 같은 가격 데이터로 계산하므로 별도 API 없이 자동으로 갱신됩니다.

## 어떻게 동작하나

1. `rs/compute_rs.py` 가 (코스피200+코스닥150+미국 S&P500+ETF) 유니버스 전체의 가격을 받아
   RS(장기·단기)를 계산 → `data/rs.json` (유니버스 전체) + `data/portfolio.json` (자동
   포트폴리오) + `data/manual_portfolio.json` (수동 포트폴리오, `data/manual.json` 기반) 생성
2. **GitHub Actions** 가 장중 매시 정각(평일)에 이 스크립트를 자동 실행 → 결과 갱신
3. **GitHub Pages** 가 `index.html`(대시보드)과 데이터를 웹에 공개
4. 대시보드에서 새로고침을 누르면 그 시점까지 Actions가 커밋해둔 **최신 결과를 다시 불러옵니다**
   (브라우저가 그 순간 시세를 직접 계산하는 게 아니라, 서버리스 구조상 "가장 최근 완료된 계산 결과"를 보여주는 방식입니다)

### 자동 포트폴리오 비중은 이렇게 산출됩니다

- **테마 = 거래소가 제공하는 업종(Sector) 분류** 기반입니다. 미국은 S&P500의 GICS 업종을 그대로
  쓰고, 한국은 KRX 세부 업종(반도체 제조업, 완제품 의약품 제조업 등 수백 개)을 AI 하드웨어·인프라/
  바이오·제약/방산 같은 투자테마 느낌의 큰 카테고리로 묶는 번역 테이블(`KR_THEME_MAP`)을 거칩니다.
  종목 하나하나가 아니라 업종 카테고리 단위로만 매핑해서 사람이 관리할 부담이 적습니다.
- **테마가 강할수록 후보 종목이 더 많습니다.** 모든 테마를 무조건 같은 수로 뽑으면 RS가 유독
  강하고 넓게 오르는 테마도 3종목으로 묶여서 "테마의 폭"이 안 드러나는 문제가 있었습니다. 그래서
  테마를 RS 강도(그 테마 안에서 RS가 양수인 종목들의 합) 순으로 상/중/하위 1/3씩 나눠, 상위
  테마는 `N_STRONG_THEME`(기본 6)개, 중위는 `N_MID_THEME`(기본 3)개, 하위는 `N_WEAK_THEME`
  (기본 1)개까지 후보로 남깁니다. RS가 마이너스인 종목/업종은 자동으로 비중 0이 됩니다.
- 업종 비중은 그 업종 후보 종목들의 RS 합에 비례, 업종 안에서는 개별 종목 RS 크기에 비례해서
  나눠 담습니다. → 시장 상황이 바뀌면(어떤 업종이 강해지거나 약해지면) 다음 실행 때 비중도
  자동으로 따라 바뀝니다.
- 후보 종목 수(`N_STRONG_THEME`/`N_MID_THEME`/`N_WEAK_THEME`)는 `rs/compute_rs.py` 상단
  상수, 환경변수, 또는 워크플로 실행 시 `--n-strong-theme`/`--n-mid-theme`/`--n-weak-theme`
  인자로 조정할 수 있습니다.

### 수동 포트폴리오는 이렇게 입력합니다

`data/manual.json` 을 GitHub에서 열어(연필 아이콘 → Edit) 이런 식으로 적고 커밋하면 됩니다:

```json
{
  "holdings": [
    {"code": "005930", "weight": 15},
    {"code": "000660", "weight": 10},
    {"code": "AAPL", "weight": 5}
  ]
}
```

이름·업종·시총·RS는 코드로 유니버스에서 자동으로 찾아 채워줍니다 — `weight`(비중)만 적으면 됩니다.
합계가 100이 아니어도 그대로 표시합니다(현금 비중을 안 적어도 되게). 유니버스 스캔에 없는 코드는
RS 없이 "–"로 표시되니, 상장된 정확한 종목코드(한국은 6자리, 미국은 티커)를 적어주세요.

RS는 종가 기반 지표라 분/초 단위 실시간은 아니고, **장중 매시 갱신**이 기본값입니다
(코스피200+코스닥150+미국 S&P500을 스캔하는 데 시간이 걸려서, 더 잦은 주기는 무료 구조상
현실적이지 않습니다 — 아래 "자주 손대는 곳" 참고).

## 설치 — 따라 하기

### 1~3단계는 기존과 동일 (저장소 생성 → 파일 업로드 → Actions 권한 켜기)

이 README와 함께 받은 폴더 구조를 그대로 GitHub 저장소에 올리고, **Settings → Actions →
General → Workflow permissions → Read and write permissions** 를 켭니다.

### 4. 토스증권 Open API — 실사용 불가 판정 (사용 안 함)

[토스증권 Open API](https://corp.tossinvest.com/ko/open-api) 연동을 시도했으나, 실제 공식
문서(`openapi.tossinvest.com/openapi-docs`) 확인 결과 **GitHub Actions와는 구조적으로
맞지 않아 사용하지 않기로 했습니다**. 이유:

- 토스 API는 **허용 IP 목록**에 등록된 IP에서만 호출을 허용하고, 나머지는 403으로 막습니다.
  그런데 GitHub Actions 러너는 실행마다 IP가 바뀌는 공유 대역이라(GitHub 공식 문서도
  "이 IP로 화이트리스트 걸지 말 것"을 권고함) 애초에 고정 등록이 불가능합니다.
- 종목마스터 API에는 업종(Sector/GICS) 정보가 없어서, 있었어도 테마 분류에는 도움이
  안 됐을 거예요.

`rs/toss_client.py`는 코드로는 남겨뒀지만(나중에 고정 IP 서버를 경유하는 식으로 우회하고
싶어질 경우를 위해), **`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` 시크릿을 등록하지 않으면
자동으로 무시되고 FinanceDataReader만 사용**합니다. 이미 등록해두셨다면 GitHub 저장소
**Settings → Secrets and variables → Actions** 에서 두 시크릿을 지워주세요(지우지 않아도
동작에는 문제없지만, 매 실행마다 실패 로그가 한 번씩 더 찍힙니다).

### 5. 첫 실행 (전체 유니버스 데이터 생성)

1. 상단 **Actions** 탭 → 왼쪽 **RS 자동 갱신** → **Run workflow** → **Run**
2. 유니버스가 코스피200+코스닥150+미국 S&P500+ETF(약 800~900종목) 정도라 첫 실행도
   보통 수 분~수십 분 안에 끝납니다. 로그에서 `[universe] KOSPI200: 200`,
   `[universe] KOSDAQ150: 150` 로 종목 수가 맞는지 확인하고, `[done] 유니버스 ... → data/rs.json`,
   `[done] 포트폴리오 ... → data/portfolio.json` 메시지를 확인하세요.

### 6. 웹에 공개 (GitHub Pages)

**Settings → Pages → Source: Deploy from a branch, Branch: main/(root)** → Save.
1~2분 뒤 `https://<아이디>.github.io/rs-dashboard/` 주소가 생깁니다.

## 자주 손대는 곳

- **갱신 주기**: `.github/workflows/rs.yml` 의 `cron`. 기본은 평일 장중(한국장+미국장) 매시 정각.
  유니버스를 줄이면(아래) 더 잦은 주기(예: 30분)도 가능합니다.
- **유니버스 범위**: `rs/compute_rs.py` 의 `build_universe()` — 한국은 KOSPI200+KOSDAQ150
  (시가총액 상위), 미국은 S&P500으로 이미 제한되어 있습니다. `_kr_listing("KOSPI", top_n=200)` /
  `_kr_listing("KOSDAQ", top_n=150)` 의 숫자를 바꾸면 범위를 넓히거나 좁힐 수 있습니다.
- **업종당 후보 종목 수(강도별)**: `N_STRONG_THEME`/`N_MID_THEME`/`N_WEAK_THEME` (기본 6/3/1)
  — `rs/compute_rs.py` 상단 또는 워크플로에서 `--n-strong-theme`/`--n-mid-theme`/
  `--n-weak-theme` 인자로.
- **동시 실행 스레드 수**: `RS_MAX_WORKERS` 환경변수 (기본 16) — 너무 크면 시세 소스가 차단할 수 있음.
- **대시보드 데이터 경로**: `index.html` 의 `CONFIG.PORTFOLIO_JSON_URL` / `CONFIG.UNIVERSE_JSON_URL`.

## 빠른 테스트

전체 대신 30종목만 빠르게 돌려보려면:
```bash
cd rs
pip install -r ../requirements.txt
python compute_rs.py --limit 30 --out ../data/rs.json --portfolio-out ../data/portfolio.json
```

## RS 산식 (IGIS 리포트와 동일)

- **장기 RS** = 3·6·9·12개월 수익률 가중평균(40·20·20·20%) → 유니버스 백분위(1~99)
- **단기 RS** = 1·2·4주 수익률 가중평균(50·30·20%) → 유니버스 백분위
- 코멧 4분면: 주도(둘 다 강함) · 개선(단기만 강함) · 약화(장기만 강함) · 소외(둘 다 약함)
- 포트폴리오 종목별 종합점수 = 0.55×장기Z + 0.45×단기Z (업종/종목 비중 산출에 사용)

## 참고

- 데이터는 FinanceDataReader(무료) 또는 토스증권 Open API에서 받습니다. 일부 종목은 데이터가
  없어 자동 제외될 수 있습니다.
- 시가총액/변동성은 소스에서 값을 못 가져오면 "–"로 표시됩니다. Fwd P/E는 현재 자동 산출되지
  않아 항상 "–"로 표시됩니다.
- 본 도구는 정보 제공용이며 투자 권유가 아닙니다.
