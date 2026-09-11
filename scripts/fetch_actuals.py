#!/usr/bin/env python3
"""
새내기주 트래커 - 실제 실적 자동 갱신 스크립트

DART(전자공시) Open API에서 종목별 실제 실적(매출액·영업이익·당기순이익)을 가져와
data/actuals.json 을 갱신한다.

- 가이던스(상장 당시 추정치)는 이 저장소가 더 이상 들고 있지 않는다 — 페이지에서
  사용자가 직접 입력하고 브라우저(localStorage)에 저장한다. 이 스크립트는 그
  가이던스와 "비교할 실제 실적"만 자동으로 채운다.
- 최근 3개 사업연도는 사업보고서(연간) 기준, 올해는 아직 사업보고서가 없으므로
  공시된 가장 최근 분기/반기보고서(누적) 기준으로 가져온다.
- 스케줄 실행 없이 수동(workflow_dispatch)으로만 돈다 — 새 분기보고서가 나올 때마다
  Actions 탭에서 "실제 실적 자동 갱신" 워크플로를 눌러 실행해달라.

필요한 것: 저장소 Settings → Secrets and variables → Actions 에 DART_API_KEY 시크릿
(https://opendart.fss.or.kr 에서 무료로 발급받은 Open API 인증키)을 등록해야 한다.

실행: python scripts/fetch_actuals.py   (표준 라이브러리만 사용, 별도 설치 불필요)
"""
import datetime
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree

DATA_DIR = "data"
COMPANIES_PATH = f"{DATA_DIR}/companies.json"
CORP_CODE_CACHE_PATH = f"{DATA_DIR}/corp_codes.json"
OUTPUT_PATH = f"{DATA_DIR}/actuals.json"

API_KEY = os.environ.get("DART_API_KEY", "").strip()
BASE = "https://opendart.fss.or.kr/api"
TIMEOUT = 30

TODAY = datetime.date.today()
ANNUAL_BSNS_YEAR = str(TODAY.year - 1)  # 최근 마감된 회계연도의 사업보고서
CURRENT_YEAR = str(TODAY.year)

# 올해분 최신 분기 데이터 — 우선순위대로 시도해서 값이 있는 것을 채택한다.
INTERIM_ATTEMPTS = [
    ("11014", f"{TODAY.year}년 3분기(누적)"),
    ("11012", f"{TODAY.year}년 반기(누적)"),
    ("11013", f"{TODAY.year}년 1분기(누적)"),
]

REV_NAMES = {"매출액", "수익(매출액)"}
OP_NAMES = {"영업이익", "영업이익(손실)"}
NI_NAMES = {"당기순이익(손실)", "당기순이익"}

# DART의 corpCode.xml 벌크 다운로드는 최근 상장한 일부 종목의 영숫자 종목코드
# (예: 0007J0)를 아직 반영하지 못하는 경우가 있다 — 개별 기업개황(company.json)
# 조회로는 종목코드가 정상적으로 확인되는데도 벌크 매핑에는 누락되는 사례.
# 이런 종목은 corp_code를 직접 알아내서 여기에 등록해두면 벌크 매핑 누락과
# 상관없이 계속 조회된다. (확인 방법: opendart_get_company_info로 corp_code
# 조회 시 종목코드가 정상 표시되면 여기에 추가)
MANUAL_CORP_CODE_OVERRIDES = {
    "0007J0": "01869710",  # 인벤테라
    "0011T0": "01279698",  # 채비
    "0117P0": "01755259",  # 피스피스스튜디오
    "0039P0": "01379129",  # 매드업
    "0156T0": "00624244",  # 에이치엘지노믹스
}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg):
    log(f"오류: {msg}")
    sys.exit(1)


def fetch_url(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "ipo-tracker-actuals/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url):
    return json.loads(fetch_url(url).decode("utf-8"))


def to_eok(amount_str):
    """'63,469,561,784' 같은 원 단위 문자열을 억원 단위 float로 변환 (소수 1자리 반올림)."""
    if amount_str is None:
        return None
    s = str(amount_str).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return round(float(s) / 1e8, 1)
    except ValueError:
        return None


def load_companies():
    if not os.path.exists(COMPANIES_PATH):
        die(f"{COMPANIES_PATH} 를 찾을 수 없습니다.")
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    tickers = []
    for x in data:
        t = (x.get("ticker") or "").strip()
        # 정식 종목코드는 6자리(숫자 또는 영숫자 혼용, 예: 0007J0)다. 영숫자 코드도
        # DART에 정상 등록된 진짜 종목코드인 경우가 많으므로(벌크 매핑만 누락되는
        # 것일 뿐) 더 이상 숫자 6자리로만 제한하지 않는다 — MANUAL_CORP_CODE_OVERRIDES
        # 로 벌크 매핑 누락을 보완한다.
        if t and len(t) == 6:
            tickers.append(t)
    return sorted(set(tickers))


def fetch_corp_codes():
    """DART corpCode.xml 벌크 다운로드 → {종목코드: corp_code} 매핑. 결과를 캐시한다."""
    if os.path.exists(CORP_CODE_CACHE_PATH):
        try:
            with open(CORP_CODE_CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                log(f"corp_code 캐시 사용 ({len(cached)}건) — 갱신하려면 {CORP_CODE_CACHE_PATH} 를 지우고 다시 실행하세요.")
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    log("DART corpCode.xml 다운로드 중…")
    url = f"{BASE}/corpCode.xml?" + urllib.parse.urlencode({"crtfc_key": API_KEY})
    raw = fetch_url(url, timeout=60)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        die("corpCode.xml 다운로드 실패 — DART_API_KEY가 올바른지 확인해주세요. 응답 앞부분: " + raw[:300].decode("utf-8", "ignore"))
    xml_bytes = zf.read(zf.namelist()[0])
    root = ElementTree.fromstring(xml_bytes)

    mapping = {}
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code and corp_code:
            mapping[stock_code] = corp_code

    with open(CORP_CODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    log(f"corp_code 매핑 {len(mapping)}건 저장 완료 ({CORP_CODE_CACHE_PATH})")
    return mapping


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def call_multi_accounts(corp_codes, bsns_year, reprt_code):
    params = {
        "crtfc_key": API_KEY,
        "corp_code": ",".join(corp_codes),
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    }
    url = f"{BASE}/fnlttMultiAcnt.json?" + urllib.parse.urlencode(params)
    try:
        payload = fetch_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        log(f"  경고: {bsns_year}/{reprt_code} 배치 조회 실패 — {e}")
        return None
    status = payload.get("status")
    if status != "000":
        if status != "013":  # 013 = 조회된 데이터 없음(정상적으로 발생 가능 — 미제출 등)
            log(f"  경고: {bsns_year}/{reprt_code} 응답 status={status} {payload.get('message')}")
        return None
    return payload.get("list", [])


def pick_metric_rows(rows):
    """corp_code -> IS(손익계산서) 행 목록. CFS(연결) 우선, 없으면 OFS(별도)."""
    by_corp = {}
    for r in rows:
        by_corp.setdefault(r["corp_code"], []).append(r)
    picked = {}
    for corp_code, corp_rows in by_corp.items():
        for fs_div in ("CFS", "OFS"):
            fs_rows = [r for r in corp_rows if r.get("fs_div") == fs_div and r.get("sj_div") == "IS"]
            if fs_rows:
                picked[corp_code] = fs_rows
                break
    return picked


def fetch_interim(corp_codes):
    """올해분 최신 분기/반기 실적. corp_code -> {rev,op,ni,period}."""
    result = {}
    remaining = list(corp_codes)
    for reprt_code, label in INTERIM_ATTEMPTS:
        if not remaining:
            break
        log(f"올해({CURRENT_YEAR}) 실적 조회 중… reprt_code={reprt_code} ({label})")
        found_this_round = []
        for batch in chunked(remaining, 100):
            rows = call_multi_accounts(batch, CURRENT_YEAR, reprt_code)
            if not rows:
                continue
            for corp_code, fs_rows in pick_metric_rows(rows).items():
                vals = {}
                for r in fs_rows:
                    name = (r.get("account_nm") or "").strip()
                    amt = to_eok(r.get("thstrm_amount"))
                    if amt is None:
                        continue
                    if name in REV_NAMES and "rev" not in vals:
                        vals["rev"] = amt
                    elif name in OP_NAMES and "op" not in vals:
                        vals["op"] = amt
                    elif name in NI_NAMES and "ni" not in vals:
                        vals["ni"] = amt
                if vals.get("rev") is not None:
                    result[corp_code] = {"period": label, "isFullYear": False, **vals}
                    found_this_round.append(corp_code)
            time.sleep(0.2)
        remaining = [c for c in remaining if c not in result]
    return result


def fetch_annual_3y(corp_codes):
    """사업보고서(11011) 1회 호출로 당기/전기/전전기 3개년을 한 번에 파싱."""
    y0 = int(ANNUAL_BSNS_YEAR)
    years_out = {}  # corp_code -> {year:{rev,op,ni}}
    period_cols = [("thstrm_amount", y0), ("frmtrm_amount", y0 - 1), ("bfefrmtrm_amount", y0 - 2)]

    log(f"연간 실적({ANNUAL_BSNS_YEAR}년 사업보고서, 3개년) 조회 중…")
    for batch in chunked(corp_codes, 100):
        rows = call_multi_accounts(batch, ANNUAL_BSNS_YEAR, "11011")
        if not rows:
            continue
        for corp_code, fs_rows in pick_metric_rows(rows).items():
            per_year = {y0: {}, y0 - 1: {}, y0 - 2: {}}
            for r in fs_rows:
                name = (r.get("account_nm") or "").strip()
                key = "rev" if name in REV_NAMES else "op" if name in OP_NAMES else "ni" if name in NI_NAMES else None
                if not key:
                    continue
                for col, yr in period_cols:
                    amt = to_eok(r.get(col))
                    if amt is not None and key not in per_year[yr]:
                        per_year[yr][key] = amt
            if per_year[y0].get("rev") is not None:
                years_out[corp_code] = per_year
        time.sleep(0.2)
    return years_out


def main():
    if not API_KEY:
        die("환경변수 DART_API_KEY가 설정되어 있지 않습니다. (저장소 Settings → Secrets → Actions 에 등록)")

    tickers = load_companies()
    log(f"종목코드 {len(tickers)}건 로드")

    corp_map = fetch_corp_codes()
    corp_map = {**corp_map, **MANUAL_CORP_CODE_OVERRIDES}  # 벌크 매핑 누락 보완(수동 등록분 우선)
    resolved = {t: corp_map[t] for t in tickers if t in corp_map}
    log(f"corp_code 매핑 성공 {len(resolved)}/{len(tickers)}건")
    corp_to_ticker = {v: k for k, v in resolved.items()}
    corp_codes = list(resolved.values())

    annual = fetch_annual_3y(corp_codes)
    interim = fetch_interim(corp_codes)

    out_years = {}
    y0 = int(ANNUAL_BSNS_YEAR)
    for corp_code, ticker in corp_to_ticker.items():
        entry = {}
        if corp_code in annual:
            for yr in (y0, y0 - 1, y0 - 2):
                vals = annual[corp_code].get(yr, {})
                if vals.get("rev") is not None:
                    entry[str(yr)] = {"period": "연간", "isFullYear": True, **vals}
        if corp_code in interim:
            entry[CURRENT_YEAR] = interim[corp_code]
        if entry:
            out_years[ticker] = entry

    output = {
        "updatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "years": out_years,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"완료: {len(out_years)}개 종목의 실적을 {OUTPUT_PATH} 에 저장했습니다.")


if __name__ == "__main__":
    main()
