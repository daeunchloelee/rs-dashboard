#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
유니버스 RS 계산기 — 코스피+코스닥+미국 상장사+주요 ETF의 상대강도(RS)를 계산한다.
IGIS 산식 동일: 장기 RS = 3·6·9·12개월 수익률 가중평균(40·20·20·20%),
              단기 RS = 1·2·4주 수익률 가중평균(50·30·20%),
유니버스 내 백분위(1~99) + z점수(코멧 축)로 변환하고 4분면 등급을 부여한다.

두 개의 결과 파일을 만든다:
  data/rs.json         전체 유니버스 RS (대시보드 "유니버스 RS" 탭)
  data/portfolio.json  단일 포트폴리오 자동 비중 (대시보드 "테마 비중"/"종목 상세" 탭)
    - 테마 = 거래소가 제공하는 업종(Sector) 분류를 그대로 사용 (수동 매핑 없음)
    - 비중 = 업종별 RS 상위 N종목(기본 3)만 후보로 남기고, 업종 비중은 업종의 RS 합에
      비례, 업종 내 개별 비중은 종목 RS 크기에 비례 (RS가 마이너스면 자동으로 비중 0)

데이터 소스: TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 설정돼 있으면 토스증권
Open API(공식, 2026-08 전고객 오픈)를 우선 사용하고, 없으면 FinanceDataReader(무료/비공식)로
자동 폴백한다. 두 백엔드 모두 이 스크립트를 실행하는 컴퓨터/러너의 인터넷 접속이 필요하다
(작성 시 사용한 샌드박스는 방화벽 때문에 두 백엔드 모두 직접 검증하지 못했다 — 최초 실행은
GitHub Actions의 workflow_dispatch로 반드시 한번 확인할 것. 로그의 [토스]/[universe] 경고를 확인).
"""
import json, time, sys, argparse, warnings, os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd
import requests
import FinanceDataReader as fdr

import toss_client

warnings.simplefilter("ignore")

# ---- KRX(data.krx.co.kr)는 Akamai WAF가 기본 UA/헤더 없는 요청(GitHub Actions 러너 등)을
# "Access Denied"로 막는 경우가 있다. 브라우저처럼 보이는 헤더를 강제로 붙여서 통과율을 높인다.
# (KRX 실데이터 요청에 흔히 필요한 Referer/X-Requested-With까지 같이 붙임)
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
_KRX_HEADERS = {
    **_BROWSER_HEADERS,
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
    "X-Requested-With": "XMLHttpRequest",
}


def _inject_headers(url, kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    extra = _KRX_HEADERS if "data.krx.co.kr" in url else _BROWSER_HEADERS
    for k, v in extra.items():
        headers.setdefault(k, v)
    kwargs["headers"] = headers
    return kwargs


_orig_requests_get = requests.get
_orig_session_request = requests.Session.request


def _patched_get(url, *args, **kwargs):
    kwargs = _inject_headers(url, kwargs)
    return _orig_requests_get(url, *args, **kwargs)


def _patched_session_request(self, method, url, *args, **kwargs):
    kwargs = _inject_headers(url, kwargs)
    return _orig_session_request(self, method, url, *args, **kwargs)


requests.get = _patched_get
requests.Session.request = _patched_session_request

# ---- RS 가중치 (거래일 기준) ----
LONG_W  = [(63, 0.40), (126, 0.20), (189, 0.20), (252, 0.20)]   # 3·6·9·12개월
SHORT_W = [(5, 0.50), (10, 0.30), (20, 0.20)]                    # 1·2·4주

# ---- 주요 ETF (미국 + 한국) — Toss/FDR 어느 백엔드를 쓰든 공통으로 유니버스에 추가 ----
ETF_US = ["SPY","QQQ","DIA","IWM","VTI","VOO","SOXX","SMH","XLK","VGT","XLF","XLE","XLV",
          "XLI","XLY","XLP","XLU","XLB","XLRE","XLC","ARKK","TAN","ICLN","LIT","IBB","XBI",
          "GLD","SLV","USO","TLT","HYG","EEM","EFA","FXI","EWY","EWJ","VNQ","SCHD","JEPI"]
ETF_KR = ["069500","229200","305540","091160","091170","305720","364980","371460","148020",
          "117460","139260","102110","233740","251340","294400","357870","456600","473460"]
# 미국 ETF 정식명 — 이 18개(코스피/코스닥과 달리)는 티커가 안 바뀌는 초대형 고정 상품이라
# 정적으로 적어둔다(한국 ETF는 신규 상장이 잦아서 대신 실시간 조회로 처리함, _kr_etf_names 참고).
ETF_US_NAMES = {
    "SPY":"SPDR S&P 500 ETF Trust", "QQQ":"Invesco QQQ Trust", "DIA":"SPDR Dow Jones Industrial Average ETF Trust",
    "IWM":"iShares Russell 2000 ETF", "VTI":"Vanguard Total Stock Market ETF", "VOO":"Vanguard S&P 500 ETF",
    "SOXX":"iShares Semiconductor ETF", "SMH":"VanEck Semiconductor ETF", "XLK":"Technology Select Sector SPDR Fund",
    "VGT":"Vanguard Information Technology ETF", "XLF":"Financial Select Sector SPDR Fund",
    "XLE":"Energy Select Sector SPDR Fund", "XLV":"Health Care Select Sector SPDR Fund",
    "XLI":"Industrial Select Sector SPDR Fund", "XLY":"Consumer Discretionary Select Sector SPDR Fund",
    "XLP":"Consumer Staples Select Sector SPDR Fund", "XLU":"Utilities Select Sector SPDR Fund",
    "XLB":"Materials Select Sector SPDR Fund", "XLRE":"Real Estate Select Sector SPDR Fund",
    "XLC":"Communication Services Select Sector SPDR Fund", "ARKK":"ARK Innovation ETF",
    "TAN":"Invesco Solar ETF", "ICLN":"iShares Global Clean Energy ETF", "LIT":"Global X Lithium & Battery Tech ETF",
    "IBB":"iShares Biotechnology ETF", "XBI":"SPDR S&P Biotech ETF", "GLD":"SPDR Gold Shares",
    "SLV":"iShares Silver Trust", "USO":"United States Oil Fund LP", "TLT":"iShares 20+ Year Treasury Bond ETF",
    "HYG":"iShares iBoxx $ High Yield Corporate Bond ETF", "EEM":"iShares MSCI Emerging Markets ETF",
    "EFA":"iShares MSCI EAFE ETF", "FXI":"iShares China Large-Cap ETF", "EWY":"iShares MSCI South Korea ETF",
    "EWJ":"iShares MSCI Japan ETF", "VNQ":"Vanguard Real Estate ETF", "SCHD":"Schwab US Dividend Equity ETF",
    "JEPI":"JPMorgan Equity Premium Income ETF",
}

# ---- 포트폴리오 자동 구성 파라미터 ----
# 예전엔 모든 업종(테마)이 무조건 top_n(3)개 후보로 똑같았다 — 그러면 RS가 유독 강하고
# 넓게 오르는 테마도 3종목으로 묶여서 "테마의 폭"이 드러나지 않는 문제가 있었다.
# 그래서 테마를 RS 강도(그 테마 안에서 RS가 양수인 종목들의 합) 순으로 3등분(상/중/하위)해서
# 강한 테마일수록 후보 종목을 더 많이 남기도록 바꿨다.
N_STRONG_THEME = int(os.environ.get("N_STRONG_THEME", 6))  # 상위 1/3 테마: 후보 종목 수
N_MID_THEME    = int(os.environ.get("N_MID_THEME", 3))     # 중위 1/3 테마: 후보 종목 수
N_WEAK_THEME   = int(os.environ.get("N_WEAK_THEME", 1))    # 하위 1/3 테마: 후보 종목 수
MAX_WORKERS = int(os.environ.get("RS_MAX_WORKERS", 16))       # 동시 가격조회 스레드 수


def _kr_etf_names():
    """ETF_KR 코드 → 실제 상품명 매핑. fdr.StockListing('ETF/KR')(네이버 ETF 목록, KRX
    endpoint가 아니라 별도 도메인)에서 실시간으로 받아온다 — 하드코딩하지 않는 이유는
    ETF 상품명이 바뀌거나 새 ETF가 추가될 수 있어서다. 실패하면 빈 dict를 반환해서
    상위에서 종목코드를 이름으로 그냥 쓰게(폴백) 한다."""
    for attempt in range(2):
        try:
            df = fdr.StockListing("ETF/KR")
            code_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            name_col = "Name" if "Name" in df.columns else df.columns[1]
            return {str(r[code_col]).zfill(6): str(r[name_col]) for _, r in df.iterrows()}
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                print("[universe] ETF/KR 이름 조회 실패, 종목코드를 이름으로 사용:", e)
    return {}


# ---- 한국 종목 테마 분류: KRX 세부 업종(수백 개, 예: '반도체 제조업', '완제품 의약품 제조업')을
# 투자테마 느낌의 큰 카테고리로 묶는 번역 테이블. 종목 하나하나를 매핑하는 게 아니라
# "업종 카테고리" 단위로만 매핑하기 때문에(개수가 훨씬 적고 잘 안 바뀜) 유지보수가 쉽다.
# 순서가 중요: 위에서부터 먼저 매칭되는 테마로 확정된다(좁은 카테고리를 위쪽에 둠).
# 여기 키워드에 안 걸리는 업종은 '기타'로 남는다 — 실행 로그에 '기타'로 빠진 원본 업종명이
# 찍히니, 필요하면 그 목록을 보고 키워드를 추가하면 된다.
KR_THEME_MAP = [
    ("2차전지·배터리",      ["2차전지", "축전지", "배터리"]),
    ("로봇·우주항공",       ["로봇", "우주", "항공기", "드론"]),
    ("방산",               ["무기", "총포", "방위산업", "군수"]),
    ("AI 하드웨어·인프라",   ["반도체", "전자부품", "디스플레이", "전자집적회로", "컴퓨터", "통신 및 방송 장비",
                             "영상 및 음향기기", "전자관"]),
    ("AI 소프트웨어·플랫폼", ["소프트웨어", "인터넷", "포털", "정보서비스", "시스템 통합", "데이터"]),
    ("전력 인프라",         ["발전기", "변전", "배전", "전기 변환", "원자력", "전선", "케이블"]),
    ("에너지 인프라·조선·건설", ["선박", "보트 건조", "조선", "건설업", "신재생", "태양광", "태양력", "풍력", "플랜트"]),
    ("바이오·제약",         ["의약", "제약", "바이오", "의료기기", "생물학적 제제", "진단"]),
    ("자동차·모빌리티",     ["자동차", "타이어"]),
    ("K-컬쳐·미디어",       ["방송", "영화", "음반", "엔터테인먼트", "화장품", "출판", "오락"]),
    ("금융",               ["은행", "증권", "보험", "카드", "금융업", "캐피탈"]),
    ("화학·소재",           ["화학물질", "석유화학", "합성고무", "플라스틱", "고무"]),
    ("철강·금속",           ["제철", "제강", "철강", "비철금속", "금속 가공"]),
    ("통신",               ["전기통신", "통신업"]),
    ("유통·소비재",         ["도매", "소매", "식품", "음료", "슈퍼마켓", "편의점"]),
]


def _map_kr_theme(raw_sector, raw_industry=""):
    text = f"{raw_sector or ''} {raw_industry or ''}"
    for theme, keywords in KR_THEME_MAP:
        if any(kw in text for kw in keywords):
            return theme
    return None  # 매칭 실패 — 호출부에서 '기타' 처리 + 원본 업종명 로깅


_KRX_CACHE_BASE = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing"
_KRX_MARKET_ID = {"KOSPI": "STK", "KOSDAQ": "KSQ", "KONEX": "KNX"}


def _krx_max_work_dt():
    """KRX가 제공하는 '가장 최근 거래일'(max_work_dt)을 조회한다. FinanceDataReader가
    fdr.StockListing('KOSPI'/'KOSDAQ') 내부에서 쓰는 것과 같은 엔드포인트. 실패하면 None을
    반환해서 호출부가 오늘 날짜부터 대신 걸어 내려가게 한다."""
    url = ("http://data.krx.co.kr/comm/bldAttendant/executeForResourceBundle.cmd"
           "?baseName=krx.mdc.i18n.component&key=B128.bld")
    try:
        r = requests.get(url, timeout=10)
        j = r.json()
        date_str = j["result"]["output"][0]["max_work_dt"]
        return datetime.strptime(date_str, "%Y%m%d")
    except Exception as e:
        print("[universe] KRX 최근거래일 조회 실패(오늘 날짜부터 대신 시도):", e)
        return None


def _fetch_krx_cache_csv(kind, start_date, max_back_days=10):
    """FinanceDataReader가 실제 데이터를 받아오는 GitHub 캐시 저장소
    (FinanceData/fdr_krx_data_cache)에서 kind('krx' 또는 'desc') CSV를 받는다.
    이 캐시 저장소는 KRX의 '최근거래일'(max_work_dt)과는 별개로 자체적으로 하루 단위로
    (비동기로) 갱신되기 때문에, 최근거래일 날짜의 파일이 아직 안 올라와 있으면 그 날짜로는
    HTTP 404가 난다 — 예전 버전은 이 404를 그냥 실패로 처리해서 한국 종목이 통째로
    빠지는 문제가 있었다. 그래서 최근거래일부터 하루씩 걸어 내려가며 실제로 존재하는
    가장 최근 캐시 파일을 찾는다(시총 순위/업종 분류는 어차피 매일 크게 안 바뀌므로
    하루이틀 전 데이터를 써도 문제없다)."""
    d = start_date
    last_err = None
    for i in range(max_back_days):
        date_str = d.strftime("%Y-%m-%d")
        url = f"{_KRX_CACHE_BASE}/{kind}/{date_str}.csv"
        try:
            dtype = ({"Code": str, "Dept": str, "ChangeCode": str, "MarketId": str}
                      if kind == "krx" else {"Code": str})
            df = pd.read_csv(url, index_col=0, dtype=dtype)
            df = df.reset_index(drop=True)
            if i > 0:
                print(f"[universe] {kind} 캐시: {start_date.strftime('%Y-%m-%d')} 없음 "
                      f"→ {date_str}로 대체")
            return df, date_str
        except Exception as e:
            last_err = e
            d = d - timedelta(days=1)
    raise last_err or RuntimeError(f"{kind} 캐시: {max_back_days}일 내 사용 가능한 파일 없음")


def _kr_listing(market, top_n=None):
    """market: 'KOSPI' 또는 'KOSDAQ'. top_n을 주면 시가총액 상위 top_n개로만 자른다
    (예: KOSPI200 = _kr_listing('KOSPI', 200), KOSDAQ150 = _kr_listing('KOSDAQ', 150)).
    fdr.StockListing()을 그대로 쓰지 않고 그 내부가 읽는 GitHub 캐시를 직접 날짜-폴백과
    함께 읽는다(위 _fetch_krx_cache_csv 참고) — 캐시 파일이 최근거래일 기준으로 아직 안
    올라와 있어서 나는 404를 재시도로는 못 피하기 때문."""
    mkt_id = _KRX_MARKET_ID.get(market)
    k = None
    last_err = None
    for attempt in range(3):
        try:
            start_date = _krx_max_work_dt() or datetime.now()
            raw, _used_date = _fetch_krx_cache_csv("krx", start_date)
            k = raw[raw["MarketId"] == mkt_id].reset_index(drop=True) if mkt_id else raw
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if k is None:
        print(f"[universe] {market} 실패(3회 재시도 후):", last_err)
        return []

    # 중요: 위 krx 캐시는 시총/가격 컬럼(Marcap 등)만 주고 업종(Sector) 컬럼이 원래 없다
    # — 그래서 이전 버전은 한국 종목이 전부 "기타"로 떴다. 업종은 반드시 별도의 desc
    # 캐시(KRX 상장회사 상세정보)에서 Code 기준으로 가져와야 한다.
    sector_map = {}
    unmapped_raw = {}  # 테마 매핑에 안 걸린 원본 KRX 업종명 → 등장 횟수 (로그 확인용)
    for attempt in range(2):
        try:
            start_date = _krx_max_work_dt() or datetime.now()
            desc_raw, _used_date = _fetch_krx_cache_csv("desc", start_date)
            desc = desc_raw[desc_raw["Market"] == market].reset_index(drop=True)
            code_col_d = "Code" if "Code" in desc.columns else desc.columns[0]
            has_sec = "Sector" in desc.columns
            has_ind = "Industry" in desc.columns
            if has_sec or has_ind:
                for _, r in desc.iterrows():
                    c = str(r[code_col_d]).zfill(6)
                    raw_sec = str(r.get("Sector")).strip() if has_sec and pd.notna(r.get("Sector")) else ""
                    raw_ind = str(r.get("Industry")).strip() if has_ind and pd.notna(r.get("Industry")) else ""
                    theme = _map_kr_theme(raw_sec, raw_ind)
                    if theme:
                        sector_map[c] = theme
                    elif raw_sec:
                        unmapped_raw[raw_sec] = unmapped_raw.get(raw_sec, 0) + 1
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                print(f"[universe] {market}-DESC(업종) 조회 실패, 업종은 '기타'로 처리:", e)
    if unmapped_raw:
        top_unmapped = sorted(unmapped_raw.items(), key=lambda x: -x[1])[:8]
        print(f"[universe] {market} 테마 미매칭(→'기타') 상위 업종명: {top_unmapped}")

    code_col = "Code" if "Code" in k.columns else k.columns[0]
    name_col = "Name" if "Name" in k.columns else k.columns[1]
    mc_col = next((c for c in ["Marcap", "MarketCap", "Amount"] if c in k.columns), None)
    if top_n and mc_col:
        k = k.sort_values(mc_col, ascending=False).head(top_n)
    elif top_n:
        k = k.head(top_n)  # 시총 컬럼이 없으면 순서 그대로 상위 top_n개
    out = []
    for _, r in k.iterrows():
        code = str(r[code_col]).zfill(6)
        sector = sector_map.get(code, "기타")
        marcap = r[mc_col] if mc_col and pd.notna(r.get(mc_col)) else None
        out.append((code, str(r[name_col]), "한국", sector, marcap))
    label = f"{market}{top_n}" if top_n else market
    matched = sum(1 for c in out if c[3] != "기타")
    print(f"[universe] {label}: {len(out)} (업종 매칭 {matched}/{len(out)})")
    return out


def _us_listing(market):
    """전체 거래소 상장사 스캔용 (기본 build_universe()는 안 씀 — 종목 수가 너무 많아서
    미국은 S&P500만 쓰기로 함). 나중에 다시 넓히고 싶으면 build_universe()에서 이 함수를
    NASDAQ/NYSE/AMEX 각각에 대해 호출해서 uni에 더하면 된다."""
    try:
        s = fdr.StockListing(market)
    except Exception as e:
        print(f"[universe] {market} 실패:", e)
        return []
    sym_col = next((c for c in ["Symbol", "Code"] if c in s.columns), s.columns[0])
    nm_col = next((c for c in ["Name"] if c in s.columns), s.columns[1])
    sec_col = next((c for c in ["Sector", "Industry"] if c in s.columns), None)
    mc_col = next((c for c in ["MarketCap", "Marcap"] if c in s.columns), None)
    out = []
    for _, r in s.iterrows():
        sector = str(r[sec_col]).strip() if sec_col and pd.notna(r.get(sec_col)) else "Other"
        marcap = r[mc_col] if mc_col and pd.notna(r.get(mc_col)) else None
        out.append((str(r[sym_col]).strip(), str(r[nm_col]), "미국", sector or "Other", marcap))
    print(f"[universe] {market}: {len(out)}")
    return out


def _sp500_constituents():
    """미국 쪽 유니버스 = S&P500만 (전체 상장사 대신). [(symbol, name, sector), ...] 반환.
    안정적인 GitHub CSV(GICS 업종 포함) 우선, 실패 시 FDR로 폴백."""
    try:
        import requests, io
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        txt = requests.get(url, timeout=20).text
        sp = pd.read_csv(io.StringIO(txt))
        sec_col = next((c for c in ["GICS Sector", "Sector"] if c in sp.columns), None)
        out = []
        for _, r in sp.iterrows():
            sector = str(r[sec_col]).strip() if sec_col and pd.notna(r.get(sec_col)) else "Other"
            out.append((str(r["Symbol"]).strip(), str(r.get("Security", r["Symbol"])), sector or "Other"))
        print(f"[universe] S&P500(CSV): {len(out)}")
        return out
    except Exception as e1:
        print("[universe] S&P500 CSV 실패, FDR 시도:", e1)
        try:
            s = fdr.StockListing("S&P500")
            sym_col = next((c for c in ["Symbol", "Code"] if c in s.columns), s.columns[0])
            nm_col = next((c for c in ["Name"] if c in s.columns), s.columns[1])
            sec_col = next((c for c in ["Sector", "Industry"] if c in s.columns), None)
            out = []
            for _, r in s.iterrows():
                sector = str(r[sec_col]).strip() if sec_col and pd.notna(r.get(sec_col)) else "Other"
                out.append((str(r[sym_col]).strip(), str(r[nm_col]), sector or "Other"))
            print(f"[universe] S&P500(FDR): {len(out)}")
            return out
        except Exception as e2:
            print("[universe] S&P500 실패:", e2)
            return []


def build_universe(limit=None):
    """[(code, name, region, sector, marcap_or_None), ...] 리스트를 반환.
    토스 크리덴셜이 있으면 토스 종목마스터를 우선 사용, 실패/미설정 시 FDR로 폴백.
    종목 수가 너무 많으면 실행 시간이 부담되니 (백엔드에 상관없이) 한국은
    KOSPI200+KOSDAQ150(시가총액 상위), 미국은 S&P500 500개로만 제한한다 — 필요하면
    아래 kr_top_codes/sp500_syms 필터를 지우면 다시 전체로 넓어진다."""
    uni = []
    used_toss = False
    sp500 = _sp500_constituents()
    sp500_syms = {sym for sym, _, _ in sp500}
    kr_top = _kr_listing("KOSPI", top_n=200) + _kr_listing("KOSDAQ", top_n=150)
    kr_top_codes = {code for code, *_ in kr_top}
    # 토스 종목마스터에는 업종(Sector) 필드가 없다(공식 문서 확인) — 그래서 토스를 쓰더라도
    # 테마 분류는 FDR/S&P500 쪽에서 이미 받아온 sector/marcap을 코드 기준으로 붙여넣는다.
    kr_meta = {code: (sector, marcap) for code, _, _, sector, marcap in kr_top}
    sp500_meta = {sym: sector for sym, _, sector in sp500}

    if toss_client.enabled():
        try:
            master = toss_client.fetch_stock_master()
            if master:
                for it in master:
                    region = "한국" if any(k in it["market"] for k in ["KOSPI", "KOSDAQ", "KRX", "KR"]) else "미국"
                    if region == "미국" and it["code"] not in sp500_syms:
                        continue  # 미국은 S&P500만
                    if region == "한국" and it["code"] not in kr_top_codes:
                        continue  # 한국은 KOSPI200+KOSDAQ150만
                    if region == "한국":
                        sector, marcap = kr_meta.get(it["code"], ("기타", None))
                    else:
                        sector, marcap = sp500_meta.get(it["code"], "Other"), None
                    uni.append((it["code"], it["name"], region, sector or "기타", marcap))
                used_toss = True
                print(f"[universe] 토스 종목마스터: {len(uni)} (미국 S&P500 / 한국 KOSPI200+KOSDAQ150로 제한, "
                      f"업종은 FDR/S&P500 메타 병합)")
        except Exception as e:
            print("[universe] 토스 종목마스터 실패, FDR로 폴백:", e)

    if not used_toss:
        uni += kr_top
        for sym, name, sector in sp500:
            uni.append((sym, name, "미국", sector, None))

    kr_etf_names = _kr_etf_names()
    for t in ETF_US: uni.append((t, ETF_US_NAMES.get(t, t), "미국", "ETF", None))
    for t in ETF_KR: uni.append((t, kr_etf_names.get(t, t), "한국", "ETF", None))

    # dedup (코드 기준, 먼저 나온 항목 유지)
    seen, out = set(), []
    for row in uni:
        code = row[0]
        if code in seen: continue
        seen.add(code); out.append(row)
    if limit: out = out[:limit]
    print(f"[universe] 총 {len(out)} 종목 (백엔드: {'토스' if used_toss else 'FinanceDataReader'})")
    return out


def weighted_return(close, weights):
    if close is None or len(close) < 25:
        return np.nan
    last = float(close.iloc[-1])
    if last <= 0: return np.nan
    acc, wsum = 0.0, 0.0
    for days, w in weights:
        if len(close) > days:
            past = float(close.iloc[-1 - days])
            if past > 0:
                acc += w * (last / past - 1.0); wsum += w
    return acc / wsum if wsum > 0 else np.nan


def realized_vol(close, window=60):
    """최근 window 거래일 일간수익률 표준편차를 연율화 (%)."""
    if close is None or len(close) < window + 1:
        return None
    r = close.iloc[-window:].pct_change().dropna()
    if len(r) < 5: return None
    return float(r.std() * (252 ** 0.5) * 100)


def fetch_close(code, start):
    if toss_client.enabled():
        s = toss_client.fetch_candles(code, days=420)
        if s is not None:
            return s
        # 토스가 이 종목에서 실패하면 FDR로 개별 폴백 (완전 실패 방지)
    for attempt in range(2):
        try:
            df = fdr.DataReader(code, start)
            if df is None or df.empty: return None
            col = "Close" if "Close" in df.columns else df.columns[-1]
            return df[col].dropna()
        except Exception:
            time.sleep(0.6)
    return None


def _process_one(args):
    code, name, region, sector, marcap, start, lag = args
    close = fetch_close(code, start)
    rl = weighted_return(close, LONG_W)
    rs_ = weighted_return(close, SHORT_W)
    if np.isnan(rl) or np.isnan(rs_):
        return None
    prev = close.iloc[:-lag] if (close is not None and len(close) > lag + 25) else None
    rlp = weighted_return(prev, LONG_W); rsp = weighted_return(prev, SHORT_W)
    if np.isnan(rlp): rlp = rl
    if np.isnan(rsp): rsp = rs_
    vol = realized_vol(close)
    return {"code": code, "name": name, "market": region, "sector": sector, "marcap": marcap,
            "rawLong": rl, "rawShort": rs_, "rawLongPrev": rlp, "rawShortPrev": rsp, "vol": vol}


def fmt_marcap(v, region):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if region == "한국":
        return f"{v/1e12:.1f}조" if v >= 1e12 else f"{v/1e8:.0f}억"
    return f"${v/1e9:.1f}B" if v < 1e12 else f"${v/1e12:.2f}T"


def classify(long_z, short_z):
    if long_z >= 0 and short_z >= 0: return "lead"
    if long_z < 0 and short_z >= 0:  return "improv"
    if long_z >= 0 and short_z < 0:  return "weak"
    return "lag"


def build_manual(df, manual_in):
    """사용자가 직접 data/manual.json 에 적어둔 {code, weight} 목록을 유니버스 df와
    코드로 매칭해서, 자동 포트폴리오와 같은 모양(themes/holdings)으로 만든다.
    - 입력값(weight)은 그대로 사용한다 — 100%로 재정규화하지 않는다 (실제 계좌를
      그대로 옮겨 적는 용도라 합계가 100이 아닐 수 있다, 예: 현금 비중 미기재).
    - df(유니버스 스캔 결과)에 없는 코드는 RS/시총 없이 "–"로 표시하고 로그에 경고.
    """
    if not os.path.exists(manual_in):
        return [], [], 0
    try:
        with open(manual_in, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[manual] {manual_in} 파싱 실패:", e)
        return [], [], 0
    entries = raw.get("holdings", []) if isinstance(raw, dict) else raw
    if not entries:
        return [], [], 0

    by_code = {str(r["code"]): r for r in df.to_dict("records")}
    hold_out = []
    missing = []
    for e in entries:
        code = str(e.get("code", "")).strip()
        try:
            weight = float(e.get("weight", 0))
        except (TypeError, ValueError):
            weight = 0.0
        if not code:
            continue
        r = by_code.get(code)
        if r is None:
            missing.append(code)
            hold_out.append({
                "region": e.get("region", "기타"), "theme": e.get("theme", "미분류"),
                "code": code, "name": e.get("name", code), "longZ": None, "shortZ": None,
                "marcap": "–", "vol": "–", "weight": round(weight, 2), "class": "none",
            })
            continue
        hold_out.append({
            "region": r["market"], "theme": r["sector"], "code": code, "name": r["name"],
            "longZ": r["longZ"], "shortZ": r["shortZ"],
            "marcap": fmt_marcap(r.get("marcap"), r["market"]),
            "vol": (f"{r['vol']:.0f}%" if r.get("vol") is not None else "–"),
            "weight": round(weight, 2),
            "class": classify(r["longZ"], r["shortZ"]),
        })
    if missing:
        print(f"[manual] 유니버스에서 못 찾은 종목코드({len(missing)}개, RS 없이 표시): {missing}")

    theme_map = {}
    for h in hold_out:
        k = (h["region"], h["theme"])
        theme_map[k] = theme_map.get(k, 0) + h["weight"]
    theme_out = [{"region": r, "theme": t, "weight": round(w, 2)} for (r, t), w in theme_map.items()]
    hold_out.sort(key=lambda h: h["weight"], reverse=True)
    return theme_out, hold_out, len(missing)


def build_portfolio(df, n_strong=N_STRONG_THEME, n_mid=N_MID_THEME, n_weak=N_WEAK_THEME):
    """업종(sector)별로 RS가 강한 테마일수록 후보 종목을 더 많이 남기고, RS 크기 비례로
    비중을 배분한다. 테마를 'RS가 양수인 종목들의 Z합'(raw_strength, 후보 수 제한 없이
    전체 기준) 순으로 줄 세워 상위 1/3은 n_strong개, 중위 1/3은 n_mid개, 하위 1/3은
    n_weak개까지 후보로 남긴다 — 테마마다 무조건 같은 개수를 뽑던 예전 방식과 다르게,
    RS가 강하고 넓게 오르는 테마일수록 자연히 더 많은 종목이 포함된다."""
    df = df.copy()
    df["combinedZ"] = 0.55 * df["longZ"] + 0.45 * df["shortZ"]

    grp_cols = ["market", "sector"]
    prelim = []
    for (region, sector), g in df.groupby(grp_cols):
        pos = g[g["combinedZ"] > 0]
        if pos.empty:
            continue
        raw_strength = float(pos["combinedZ"].sum())  # 테마의 '진짜' 강도(후보 수 제한 전, 전체 양수 합)
        prelim.append({"region": region, "theme": sector, "pos": pos, "raw_strength": raw_strength})

    # 강도 순으로 정렬 후 상/중/하위 1/3씩 후보 종목 수를 다르게 배정
    prelim.sort(key=lambda t: -t["raw_strength"])
    n = len(prelim)
    theme_rows = []
    for i, t in enumerate(prelim):
        if n <= 2:
            n_cand = n_strong if i == 0 else n_weak
        else:
            tier = i / n  # 0에 가까울수록 강한 테마
            n_cand = n_strong if tier < 1 / 3 else (n_mid if tier < 2 / 3 else n_weak)
        cand = t["pos"].sort_values("combinedZ", ascending=False).head(n_cand)
        strength = float(cand["combinedZ"].sum())
        theme_rows.append({"region": t["region"], "theme": t["theme"], "strength": strength, "rows": cand})

    total_strength = sum(t["strength"] for t in theme_rows) or 1.0
    theme_out, hold_out = [], []
    for t in theme_rows:
        theme_weight = t["strength"] / total_strength * 100
        theme_out.append({"region": t["region"], "theme": t["theme"], "weight": round(theme_weight, 2)})
        for _, r in t["rows"].iterrows():
            share = float(r["combinedZ"]) / t["strength"]
            w = theme_weight * share
            hold_out.append({
                "region": t["region"], "theme": t["theme"], "code": r["code"], "name": r["name"],
                "longZ": r["longZ"], "shortZ": r["shortZ"],
                "marcap": fmt_marcap(r.get("marcap"), t["region"]),
                "vol": (f"{r['vol']:.0f}%" if r.get("vol") is not None else "–"),
                "weight": round(w, 2),
                "class": classify(r["longZ"], r["shortZ"]),
            })
    hold_out.sort(key=lambda h: h["weight"], reverse=True)
    return theme_out, hold_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="테스트용 종목 수 제한")
    ap.add_argument("--lag", type=int, default=2, help="RS Δ 비교 시점(거래일 전)")
    ap.add_argument("--out", default="data/rs.json")
    ap.add_argument("--portfolio-out", default="data/portfolio.json")
    ap.add_argument("--manual-in", default="data/manual.json", help="사용자가 직접 입력한 종목/비중")
    ap.add_argument("--manual-out", default="data/manual_portfolio.json")
    ap.add_argument("--n-strong-theme", type=int, default=N_STRONG_THEME, help="상위 1/3 강도 테마의 후보 종목 수")
    ap.add_argument("--n-mid-theme", type=int, default=N_MID_THEME, help="중위 1/3 강도 테마의 후보 종목 수")
    ap.add_argument("--n-weak-theme", type=int, default=N_WEAK_THEME, help="하위 1/3 강도 테마의 후보 종목 수")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    start = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
    uni = build_universe(args.limit)
    jobs = [(code, name, region, sector, marcap, start, args.lag)
            for code, name, region, sector, marcap in uni]

    recs = []
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_process_one, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r is None:
                fail += 1
            else:
                ok += 1
                recs.append(r)
            if i % 200 == 0:
                print(f"  ...{i}/{len(jobs)}  (ok {ok} / fail {fail})")

    if not recs:
        print("[error] 가격을 가져온 종목이 없습니다."); sys.exit(1)

    df = pd.DataFrame(recs)

    def zscale(s):
        sd = s.std(ddof=0) or 1.0
        return ((s - s.mean()) / sd * 10).clip(-40, 40)

    def pct99(s):
        return (s.rank(pct=True) * 98 + 1).round().astype(int)

    df["longRS"]  = pct99(df["rawLong"])
    df["shortRS"] = pct99(df["rawShort"])
    df["longZ"]   = zscale(df["rawLong"]).round(1)
    df["shortZ"]  = zscale(df["rawShort"]).round(1)
    lp = pct99(df["rawLongPrev"]); sp = pct99(df["rawShortPrev"])
    df["dLong"]  = (df["longRS"]  - lp).astype(int)
    df["dShort"] = (df["shortRS"] - sp).astype(int)
    df["pLongZ"]  = zscale(df["rawLongPrev"]).round(1)
    df["pShortZ"] = zscale(df["rawShortPrev"]).round(1)
    df["class"] = df.apply(lambda r: classify(r["longZ"], r["shortZ"]), axis=1)

    updated = datetime.now().isoformat(timespec="minutes")
    uni_payload = {
        "updated": updated, "count": int(len(df)), "lag": args.lag,
        "weights": {"long": LONG_W, "short": SHORT_W},
        "items": df[["code","name","market","sector","longZ","shortZ","longRS","shortRS",
                     "class","dLong","dShort","pLongZ","pShortZ"]]
                   .sort_values(["longRS","shortRS"], ascending=False)
                   .to_dict(orient="records"),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(uni_payload, f, ensure_ascii=False, separators=(",", ":"))

    theme_out, hold_out = build_portfolio(df, args.n_strong_theme, args.n_mid_theme, args.n_weak_theme)
    pf_payload = {
        "updated": updated,
        "themeTiers": {"strong": args.n_strong_theme, "mid": args.n_mid_theme, "weak": args.n_weak_theme},
        "backend": "toss" if toss_client.enabled() else "fdr",
        "themes": theme_out, "holdings": hold_out,
    }
    os.makedirs(os.path.dirname(args.portfolio_out) or ".", exist_ok=True)
    with open(args.portfolio_out, "w", encoding="utf-8") as f:
        json.dump(pf_payload, f, ensure_ascii=False, separators=(",", ":"))

    man_theme_out, man_hold_out, man_missing = build_manual(df, args.manual_in)
    man_payload = {"updated": updated, "themes": man_theme_out, "holdings": man_hold_out}
    os.makedirs(os.path.dirname(args.manual_out) or ".", exist_ok=True)
    with open(args.manual_out, "w", encoding="utf-8") as f:
        json.dump(man_payload, f, ensure_ascii=False, separators=(",", ":"))

    dist = df["class"].value_counts().to_dict()
    print(f"[done] 유니버스 {len(df)}종목 → {args.out} (성공 {ok}/실패 {fail}) 분면 {dist}")
    print(f"[done] 자동 포트폴리오 {len(hold_out)}종목 · {len(theme_out)}개 업종 → {args.portfolio_out}")
    print(f"[done] 수동 포트폴리오 {len(man_hold_out)}종목(미매칭 {man_missing}) → {args.manual_out}")


if __name__ == "__main__":
    main()
