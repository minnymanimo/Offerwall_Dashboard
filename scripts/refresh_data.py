#!/usr/bin/env python3
"""
오퍼월 광고 매출 대시보드 — 데이터 갱신 스크립트 (GitHub Actions에서 실행)

세 개의 JSON을 만듭니다:
- data.json      : 벤더 통합 일별 매출 (NBT_Adison, Buzzvil, APCORN_SSP, ADOP, Mobwith_A)
- mobwith-a.json  : Mobwith A 일별 상세 지표(노출수/클릭수/CTR/CPC/정산금액/eCPM)
- mobwith-b.json  : Mobwith B 지면별 스냅샷(노출수/클릭수/CTR/CPC/정산금액/eCPM)

구글시트를 공개 gviz 엔드포인트로 읽어옵니다.

전제조건: 이 스프레드시트가 "링크가 있는 모든 사용자: 뷰어"로 공유되어 있어야 합니다
(gviz 엔드포인트는 인증 없이 호출되므로, 시트 자체가 링크 공개 상태가 아니면 빈 결과가 옵니다).

ADPOPCORN_SSP 탭은 APCORN_SSP와 값이 중복되는 레거시 탭으로 판단되어 제외했습니다.
APCORN_SSP(media_cost)·ADOP(mediaRevNo)는 원본이 USD라, Frankfurter(ECB 기준) 당일
환율로 원화 환산합니다. 환율 조회가 실패하면 자동으로 USD 원본 표시로 폴백합니다.
"""
import json
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime, timedelta

SPREADSHEET_ID = '1k4HyjTk6SOAcL9KGcs1tka8s_t_7x9Knm8C5xBzu0Fc'

FX_BASE = 'USD'
FX_QUOTE = 'KRW'

VENDOR_CONFIG = {
    'NBT_Adison': {'label': 'NBT 애디슨', 'date_col': '날짜', 'value_col': '매출',
                   'unit': '매출 (원)', 'currency': 'KRW'},
    'Buzzvil':    {'label': 'Buzzvil', 'date_col': 'date', 'value_col': 'revenue',
                   'unit': 'revenue', 'currency': 'KRW'},
    'APCORN_SSP': {'label': 'APCORN SSP', 'date_col': 'date', 'value_col': 'media_cost',
                   'unit': 'media_cost', 'unit_krw': 'media_cost (원화 환산)', 'currency': 'USD'},
    'ADOP':       {'label': 'ADOP', 'date_col': 'date', 'value_col': 'mediaRevNo',
                   'unit': 'mediaRevNo', 'unit_krw': 'mediaRevNo (원화 환산)', 'currency': 'USD'},
    'Mobwith A':  {'label': 'Mobwith A', 'date_col': '날 짜', 'value_col': '정산금액',
                   'unit': '정산금액 (원)', 'currency': 'KRW'},
    'ADPOPCORN_Offerwall': {'label': 'ADPOPCORN 오퍼월', 'date_col': 'date', 'value_col': 'total_revenue',
                            'unit': 'total_revenue (원, AOS+iOS 합산)', 'currency': 'KRW'},
}

# Mobwith A 상세 지표(일별)에서 쓰는 원본 컬럼명 — 정산금액은 VENDOR_CONFIG와 공유
MOBWITH_A_SHEET = 'Mobwith A'
MOBWITH_A_COLS = {'date': '날 짜', 'impressions': '노출수', 'clicks': '클릭수', 'revenue': '정산금액'}

# Mobwith B — 날짜×지면 구조 (2026-08-14 시트 개편: 지면별 스냅샷 -> 일자별 매트릭스)
MOBWITH_B_SHEET = 'Mobwith B'
MOBWITH_B_COLS = {
    'date': '일자(Date)', 'id': 's값(Placement ID)', 'name': '지면명 (Placement Name)',
    'os': 'OS (Platform)', 'impressions': '노출수', 'clicks': '클릭수', 'revenue': '정산금액',
}

# gviz 응답은 "google.visualization.Query.setResponse({...});" 형태의 JSONP 래퍼로 옴
GVIZ_RESPONSE_RE = re.compile(r'^[^(]*\((.*)\);?\s*$', re.DOTALL)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8')


def fetch_sheet_rows(sheet_name):
    """공개 gviz JSON 엔드포인트에서 시트 하나의 (컬럼 라벨, 행 데이터)를 가져옴."""
    url = ('https://docs.google.com/spreadsheets/d/{}/gviz/tq?'
           'tqx=out:json&headers=1&sheet={}').format(
        SPREADSHEET_ID, urllib.parse.quote(sheet_name))
    raw = http_get(url)
    m = GVIZ_RESPONSE_RE.match(raw)
    if not m:
        raise RuntimeError('시트 "%s"에서 예상치 못한 gviz 응답 형식' % sheet_name)
    payload = json.loads(m.group(1))
    table = payload.get('table', {})
    cols = [c.get('label') or '' for c in table.get('cols', [])]
    rows = []
    for r in table.get('rows', []):
        cells = r.get('c') or []
        row = [(cell.get('v') if cell else None) for cell in cells]
        while len(row) < len(cols):
            row.append(None)
        rows.append(row)
    return cols, rows


def parse_gviz_value(v):
    """gviz의 'v' 필드를 파이썬 값으로. 날짜는 'Date(Y,M,D)' 문자열(월은 0-index)."""
    if v is None:
        return None
    if isinstance(v, str) and v.startswith('Date('):
        nums = [int(x) for x in re.findall(r'-?\d+', v)]
        y, mo, d = nums[0], nums[1] + 1, nums[2]
        return date(y, mo, d)
    return v


def normalize_date(v):
    v = parse_gviz_value(v)
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        s = str(int(round(v)))
        if len(s) != 8:
            return None
        return '%s-%s-%s' % (s[0:4], s[4:6], s[6:8])
    if isinstance(v, str) and v.strip():
        t = v.strip().replace('.', '-').replace('/', '-')
        parts = t.split('-')
        if len(parts) != 3:
            return None
        if len(parts[0]) == 4:
            y, mo, d = parts
        elif len(parts[2]) == 4:
            mo, d, y = parts
        else:
            return None
        try:
            return '%04d-%02d-%02d' % (int(y), int(mo), int(d))
        except ValueError:
            return None
    return None


def read_vendor_series(sheet_name, date_col, value_col):
    cols, rows = fetch_sheet_rows(sheet_name)
    if date_col not in cols or value_col not in cols:
        return {}
    di, vi = cols.index(date_col), cols.index(value_col)
    by_date = {}
    for row in rows:
        if di >= len(row) or vi >= len(row):
            continue
        d = normalize_date(row[di])
        if not d:
            continue
        v = parse_gviz_value(row[vi])
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        by_date[d] = float(v)  # 중복 날짜는 마지막 값으로 덮어씀 (실측상 값 동일)
    return by_date


def read_sheet_rows_by_cols(sheet_name, cols_map):
    """cols_map: {key: 헤더라벨}. 시트에서 해당 컬럼들만 뽑아 {key: 원시값, ...} 딕셔너리
    리스트로 반환한다 (parse_gviz_value 적용 전 원시값). 필요한 헤더가 하나라도 없으면
    빈 리스트를 반환해 상위 로직이 안전하게 폴백하도록 한다. 모든 값이 비어있는 행
    (시트 끝의 빈 줄 등)은 건너뛴다."""
    cols, rows = fetch_sheet_rows(sheet_name)
    idx = {}
    for key, label in cols_map.items():
        if label not in cols:
            return []
        idx[key] = cols.index(label)
    out = []
    for row in rows:
        if all((row[i] is None) for i in idx.values() if i < len(row)):
            continue
        out.append({key: (row[i] if i < len(row) else None) for key, i in idx.items()})
    return out


def _num_or_none(v):
    v = parse_gviz_value(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def build_mobwith_a_payload(fetch_fn=None):
    """Mobwith A 일별 상세 지표: 노출수/클릭수/정산금액(원본) + CTR/CPC/eCPM(파생값).
    비율 지표는 항상 노출수·클릭수·정산금액의 합에서 계산해, 기간 평균을 낼 때도
    '일별 비율의 단순평균'이 아니라 올바른 가중평균이 되도록 프론트에서 재사용한다."""
    fetch_fn = fetch_fn or read_sheet_rows_by_cols
    rows = fetch_fn(MOBWITH_A_SHEET, MOBWITH_A_COLS)

    by_date = {}
    for r in rows:
        d = normalize_date(r.get('date'))
        imp, clk, rev = _num_or_none(r.get('impressions')), _num_or_none(r.get('clicks')), _num_or_none(r.get('revenue'))
        if not d or imp is None or clk is None or rev is None:
            continue
        by_date[d] = (imp, clk, rev)  # 중복 날짜는 마지막 값으로 덮어씀

    if not by_date:
        return {'dates': [], 'impressions': [], 'clicks': [], 'revenue': [],
                'ctr': [], 'cpc': [], 'ecpm': [], 'gaps': [], 'generatedAt': now_str()}

    date_keys = sorted(by_date.keys())
    dates = build_date_range(date_keys[0], date_keys[-1])

    impressions = [by_date[d][0] if d in by_date else None for d in dates]
    clicks = [by_date[d][1] if d in by_date else None for d in dates]
    revenue = [by_date[d][2] if d in by_date else None for d in dates]
    ctr = [(c / i) if (i) else None for i, c in zip(impressions, clicks)]
    cpc = [(r / c) if (c) else None for c, r in zip(clicks, revenue)]
    ecpm = [(r / i * 1000) if (i) else None for i, r in zip(impressions, revenue)]

    return {
        'dates': dates,
        'impressions': impressions, 'clicks': clicks, 'revenue': revenue,
        'ctr': ctr, 'cpc': cpc, 'ecpm': ecpm,
        'gaps': find_gaps(revenue),
        'generatedAt': now_str(),
    }


def clean_placement_name(n):
    """'\xa0상세보기' 접미사(모비위드 리포트의 상세 링크 라벨) 제거."""
    n = n.replace('\xa0', ' ').strip()
    n = re.sub(r'\s*상세보기\s*$', '', n)
    return n.strip()


def build_mobwith_b_payload(fetch_fn=None):
    """Mobwith B — 일자×지면 매트릭스. s값(지면 ID)을 기준 키로 쓴다:
    지면명 문자열은 날짜마다 '상세보기' 접미사 유무가 갈릴 수 있어 이름만으로
    묶으면 같은 지면이 둘로 쪼개질 수 있다. 비율 지표(CTR/CPC/eCPM)는 여기서
    계산하지 않고 원본(노출수/클릭수/정산금액)만 내보낸다 — 프론트에서 선택
    기간에 맞게 합산 후 계산해야 정확하기 때문."""
    fetch_fn = fetch_fn or read_sheet_rows_by_cols
    rows = fetch_fn(MOBWITH_B_SHEET, MOBWITH_B_COLS)

    dates_set = set()
    by_id = {}
    for r in rows:
        d = normalize_date(r.get('date'))
        pid_raw = parse_gviz_value(r.get('id'))
        imp, clk, rev = _num_or_none(r.get('impressions')), _num_or_none(r.get('clicks')), _num_or_none(r.get('revenue'))
        if not d or pid_raw is None or imp is None or clk is None or rev is None:
            continue
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        name_raw = r.get('name')
        name = clean_placement_name(name_raw) if isinstance(name_raw, str) else str(pid)
        os_raw = r.get('os')
        os_val = os_raw if isinstance(os_raw, str) and os_raw.strip() else None

        dates_set.add(d)
        entry = by_id.setdefault(pid, {'id': pid, 'name': name, 'os': os_val, 'daily': {}})
        entry['daily'][d] = (imp, clk, rev)  # 중복 date+id는 마지막 값으로 덮어씀
        entry['name'] = name  # 최신 행의 이름/OS로 갱신 (표기 차이 흡수)
        if os_val:
            entry['os'] = os_val

    if not dates_set:
        return {'dates': [], 'placements': [], 'generatedAt': now_str()}

    dates = build_date_range(min(dates_set), max(dates_set))
    placements = []
    for pid, e in by_id.items():
        placements.append({
            'id': pid, 'name': e['name'], 'os': e['os'],
            'impressions': [e['daily'][d][0] if d in e['daily'] else None for d in dates],
            'clicks': [e['daily'][d][1] if d in e['daily'] else None for d in dates],
            'revenue': [e['daily'][d][2] if d in e['daily'] else None for d in dates],
        })
    placements.sort(key=lambda p: -sum(v for v in p['revenue'] if v is not None))

    return {'dates': dates, 'placements': placements, 'generatedAt': now_str()}



def build_date_range(start_s, end_s):
    start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def find_gaps(arr):
    gaps, i, n = [], 0, len(arr)
    while i < n:
        if arr[i] is None:
            j = i
            while j < n and arr[j] is None:
                j += 1
            gaps.append([i, j - 1])
            i = j
        else:
            i += 1
    return gaps


def fetch_usd_krw_rates(start_s, end_s):
    url = 'https://api.frankfurter.dev/v1/%s..%s?base=%s&symbols=%s' % (
        start_s, end_s, FX_BASE, FX_QUOTE)
    try:
        data = json.loads(http_get(url, timeout=20))
        rates = {}
        for d, obj in (data.get('rates') or {}).items():
            if isinstance(obj, dict) and isinstance(obj.get(FX_QUOTE), (int, float)):
                rates[d] = obj[FX_QUOTE]
        return rates
    except Exception:
        return {}


def fill_rate_series(dates, rate_map):
    out, last = {}, None
    for d in dates:
        if d in rate_map:
            last = rate_map[d]
        out[d] = last
    first_known = next((out[d] for d in dates if out[d] is not None), None)
    if first_known is not None:
        for d in dates:
            if out[d] is None:
                out[d] = first_known
            else:
                break
    return out


def now_str():
    return (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')  # KST


def build_payload(fetch_sheet_fn=read_vendor_series, fetch_fx_fn=fetch_usd_krw_rates):

    raw_by_vendor, all_dates = {}, set()
    for key, cfg in VENDOR_CONFIG.items():
        by_date = fetch_sheet_fn(key, cfg['date_col'], cfg['value_col'])
        raw_by_vendor[key] = by_date
        all_dates.update(by_date.keys())

    date_keys = sorted(all_dates)
    if not date_keys:
        return {'dates': [], 'vendors': {}, 'generatedAt': now_str(), 'fxAvailable': False}

    dates = build_date_range(date_keys[0], date_keys[-1])

    needs_fx = any(cfg['currency'] == 'USD' for cfg in VENDOR_CONFIG.values())
    raw_rates = fetch_fx_fn(dates[0], dates[-1]) if needs_fx else {}
    fx_available = bool(raw_rates)
    rate_by_date = fill_rate_series(dates, raw_rates) if fx_available else {}

    vendors = {}
    for key, cfg in VENDOR_CONFIG.items():
        by_date = raw_by_vendor[key]
        arr_raw = [by_date.get(d) for d in dates]
        is_usd = cfg['currency'] == 'USD'
        converted = is_usd and fx_available
        if converted:
            arr = [(v * rate_by_date[d]) if (v is not None and rate_by_date.get(d)) else None
                   for v, d in zip(arr_raw, dates)]
        else:
            arr = arr_raw

        gaps = find_gaps(arr)
        present = [(i, v) for i, v in enumerate(arr) if v is not None]
        total = sum(v for _, v in present)
        latest = present[-1] if present else None
        prev = present[-2] if len(present) >= 2 else None

        vendors[key] = {
            'label': cfg['label'],
            'unit': cfg.get('unit_krw') if converted else cfg['unit'],
            'currency': 'KRW' if converted else cfg['currency'],
            'converted': converted,
            'data': arr,
            'dataUsd': arr_raw if is_usd else None,
            'gaps': gaps,
            'total': total,
            'latestDate': dates[latest[0]] if latest else None,
            'latestVal': latest[1] if latest else None,
            'latestValUsd': arr_raw[latest[0]] if (is_usd and latest) else None,
            'prevVal': prev[1] if prev else None,
            'prevValUsd': arr_raw[prev[0]] if (is_usd and prev) else None,
            'fxRateAtLatest': rate_by_date.get(dates[latest[0]]) if (converted and latest) else None,
            'daysWithData': len(present),
            'daysMissing': len(arr) - len(present),
        }

    return {'dates': dates, 'vendors': vendors, 'generatedAt': now_str(),
            'fxAvailable': fx_available, 'needsFx': needs_fx}


if __name__ == '__main__':
    payload = build_payload()
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    print('wrote data.json: %d dates, %d vendors' % (
        len(payload.get('dates', [])), len(payload.get('vendors', {}))))

    mobwith_a = build_mobwith_a_payload()
    with open('mobwith-a.json', 'w', encoding='utf-8') as f:
        json.dump(mobwith_a, f, ensure_ascii=False)
    print('wrote mobwith-a.json: %d dates' % len(mobwith_a.get('dates', [])))

    mobwith_b = build_mobwith_b_payload()
    with open('mobwith-b.json', 'w', encoding='utf-8') as f:
        json.dump(mobwith_b, f, ensure_ascii=False)
    print('wrote mobwith-b.json: %d placements' % len(mobwith_b.get('placements', [])))
