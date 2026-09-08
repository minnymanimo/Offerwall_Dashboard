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
    'NBT_Adison': {'label': 'NBT 애디슨', 'date_col': '날짜', 'group': 'Offerwall',
                   'formula': {'type': 'subtract', 'cols': ['매출', '지급 리워드']},
                   'unit': '매출 - 지급 리워드 (원)', 'currency': 'KRW'},
    'Buzzvil':    {'label': 'Buzzvil', 'date_col': 'date', 'group': 'Offerwall',
                   'formula': {'type': 'subtract', 'cols': ['revenue', 'cost']},
                   'unit': 'revenue - cost', 'currency': 'KRW'},
    'APCORN_SSP': {'label': 'APCORN SSP', 'date_col': 'date', 'group': 'AdNetwork',
                   'formula': {'type': 'multiply', 'col': 'media_cost', 'factor': 0.8},
                   'unit': 'media_cost × 80%', 'unit_krw': 'media_cost × 80% (원화 환산)', 'currency': 'USD'},
    'ADOP':       {'label': 'ADOP(테크랩스)', 'date_col': 'date', 'value_col': 'mediaRevNo', 'group': 'AdNetwork',
                   'unit': 'mediaRevNo', 'unit_krw': 'mediaRevNo (원화 환산)', 'currency': 'USD'},
    'Mobwith A':  {'label': 'Mobwith (SSP)', 'date_col': '날 짜', 'value_col': '정산금액', 'group': 'AdNetwork',
                   'unit': '정산금액 (원)', 'currency': 'KRW'},
    'ADPOPCORN_Offerwall': {'label': 'ADPOPCORN 오퍼월', 'date_col': 'date', 'group': 'Offerwall',
                   'formula': {'type': 'multiply', 'col': 'total_revenue', 'factor': 0.6},
                   'unit': 'total_revenue × 60%', 'currency': 'KRW'},
}

# Mobwith A/C 상세 지표(일별). 2026-09 개편: A는 SSP만, C는 직광고만 담는다.
# (원래 A에 합쳐져 있던 직광고 수치를 C로 분리) 두 시트는 컬럼 구조가 동일하다.
MOBWITH_A_SHEET = 'Mobwith A'
MOBWITH_C_SHEET = 'Mobwith C'
MOBWITH_A_COLS = {'date': '날 짜', 'impressions': '노출수', 'clicks': '클릭수', 'revenue': '정산금액'}

# Mobwith B — 날짜×지면 구조. 2026-09 개편으로 '광고유형'(SSP/직광고) 컬럼이 추가됨.
MOBWITH_B_SHEET = 'Mobwith B'
MOBWITH_B_COLS = {
    'date': '일자(Date)', 'id': 's값(Placement ID)', 'name': '지면명 (Placement Name)',
    'os': 'OS (Platform)', 'adtype': '광고유형',
    'impressions': '노출수', 'clicks': '클릭수', 'revenue': '정산금액',
}
# 대조 허용 오차(원). 수기 이관·반올림에서 오는 1원 미만 차이는 경고하지 않는다.
RECONCILE_TOLERANCE = 1.0

# gviz 응답은 "google.visualization.Query.setResponse({...});" 형태의 JSONP 래퍼로 옴
GVIZ_RESPONSE_RE = re.compile(r'^[^(]*\((.*)\);?\s*$', re.DOTALL)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8')


_SHEET_CACHE = {}


def fetch_sheet_rows(sheet_name):
    """공개 gviz JSON 엔드포인트에서 시트 하나의 (컬럼 라벨, 행 데이터)를 가져옴.

    한 번 실행하는 동안 같은 시트를 두 번 이상 읽는 경우가 있어(예: Mobwith A는
    통합 매출용과 상세 페이지용으로 각각 필요) 결과를 캐시해 네트워크 요청을 줄인다.
    스크립트는 매 실행마다 새 프로세스로 뜨므로 캐시가 오래 남을 걱정은 없다."""
    if sheet_name in _SHEET_CACHE:
        return _SHEET_CACHE[sheet_name]
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
    _SHEET_CACHE[sheet_name] = (cols, rows)
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


def read_vendor_series_for_vendor(key, cfg):
    """VENDOR_CONFIG 항목 하나를 읽어 {날짜: 값} 딕셔너리로 반환한다.

    cfg에 'formula'가 있으면(뺄셈/곱셈) 필요한 컬럼을 전부 읽어 계산하고, 계산에
    필요한 값 중 하나라도 비어 있으면 그 날짜는 아예 결과에서 뺀다(예: '지급 리워드'가
    비어있는데 0으로 치면 매출이 실제보다 부풀려 보이므로) — 대시보드에는 그 날이
    다른 날들과 마찬가지로 '데이터 없음(공백)'으로 나타난다.
    formula가 없으면 기존처럼 단일 컬럼(value_col)을 그대로 읽는다."""
    date_col = cfg['date_col']
    if 'formula' not in cfg:
        return read_vendor_series(key, date_col, cfg['value_col'])

    formula = cfg['formula']
    needed_cols = list(formula['cols']) if formula['type'] == 'subtract' else [formula['col']]
    cols_map = {'__date__': date_col}
    for c in needed_cols:
        cols_map[c] = c
    rows = read_sheet_rows_by_cols(key, cols_map)

    by_date = {}
    for r in rows:
        d = normalize_date(r.get('__date__'))
        if not d:
            continue
        vals = [_num_or_none(r.get(c)) for c in needed_cols]
        if any(v is None for v in vals):
            continue
        if formula['type'] == 'subtract':
            by_date[d] = vals[0] - vals[1]
        else:  # multiply
            by_date[d] = vals[0] * formula['factor']
    return by_date


def _read_daily_sheet(sheet_name, fetch_fn):
    """A/C처럼 '날짜별 1행' 구조인 시트를 {날짜: (노출,클릭,정산)}으로 읽는다."""
    by_date = {}
    for r in fetch_fn(sheet_name, MOBWITH_A_COLS):
        d = normalize_date(r.get('date'))
        imp = _num_or_none(r.get('impressions'))
        clk = _num_or_none(r.get('clicks'))
        rev = _num_or_none(r.get('revenue'))
        if not d or imp is None or clk is None or rev is None:
            continue
        by_date[d] = (imp, clk, rev)  # 중복 날짜는 마지막 값으로 덮어씀
    return by_date


def _series_from(by_date, dates):
    """날짜축에 맞춰 시계열로 펼치고 비율 지표를 파생한다.
    비율은 항상 노출·클릭·정산의 합에서 계산해, 프론트에서 기간 가중평균을 낼 때도
    '일별 비율의 단순평균'이 되지 않도록 한다."""
    imp = [by_date[d][0] if d in by_date else None for d in dates]
    clk = [by_date[d][1] if d in by_date else None for d in dates]
    rev = [by_date[d][2] if d in by_date else None for d in dates]
    return {
        'impressions': imp, 'clicks': clk, 'revenue': rev,
        'ctr':  [(c / i) if i else None for i, c in zip(imp, clk)],
        'cpc':  [(r / c) if c else None for c, r in zip(clk, rev)],
        'ecpm': [(r / i * 1000) if i else None for i, r in zip(imp, rev)],
        'gaps': find_gaps(rev),
    }


def build_mobwith_daily_payload(fetch_fn=None):
    """Mobwith 일별 지표를 SSP(A) / 직광고(C) / 전체(A+C) 세 벌로 만든다.

    통합 매출 대시보드에는 SSP만 반영되지만(직광고는 애드네트워크 매출이 아니므로),
    Mobwith 상세 화면에서는 셋 다 보여주기 위해 한 파일에 담는다.
    전체는 두 시트를 날짜별로 더해서 만들며, 한쪽만 값이 있는 날은 그 값만 쓴다
    (양쪽 다 없는 날만 공백)."""
    fetch_fn = fetch_fn or read_sheet_rows_by_cols
    ssp_by_date = _read_daily_sheet(MOBWITH_A_SHEET, fetch_fn)
    dir_by_date = _read_daily_sheet(MOBWITH_C_SHEET, fetch_fn)

    all_keys = sorted(set(ssp_by_date) | set(dir_by_date))
    if not all_keys:
        empty = {'impressions': [], 'clicks': [], 'revenue': [],
                 'ctr': [], 'cpc': [], 'ecpm': [], 'gaps': []}
        return {'dates': [], 'series': {'ssp': dict(empty), 'direct': dict(empty), 'all': dict(empty)},
                'generatedAt': now_str()}

    dates = build_date_range(all_keys[0], all_keys[-1])

    combined = {}
    for d in dates:
        a, c = ssp_by_date.get(d), dir_by_date.get(d)
        if a is None and c is None:
            continue
        a = a or (0.0, 0.0, 0.0)
        c = c or (0.0, 0.0, 0.0)
        combined[d] = (a[0] + c[0], a[1] + c[1], a[2] + c[2])

    return {
        'dates': dates,
        'series': {
            'ssp': _series_from(ssp_by_date, dates),
            'direct': _series_from(dir_by_date, dates),
            'all': _series_from(combined, dates),
        },
        'generatedAt': now_str(),
    }


def clean_placement_name(n):
    """'\xa0상세보기' 접미사(모비위드 리포트의 상세 링크 라벨) 제거."""
    n = n.replace('\xa0', ' ').strip()
    n = re.sub(r'\s*상세보기\s*$', '', n)
    return n.strip()


def build_mobwith_b_payload(fetch_fn=None):
    """Mobwith B — 일자 x 지면 x 광고유형(SSP/직광고) 매트릭스.

    [빠진 row를 0으로 채우는 규칙]
    시트에는 "값이 전부 0인 날은 row를 아예 안 만든" 구간이 있다(8월). 이걸 그냥
    결측으로 두면 실제로는 0원인 날이 '데이터 없음'이 되어 그래프가 끊기고 변화율
    계산에서도 빠진다. 그렇다고 무조건 0으로 채우면 이번엔 '중지된 지면'이 영원히
    0원 행으로 남는다. 그래서 지면마다 활동 구간(첫 row 날짜 ~ 마지막 row 날짜)을
    잡고, 그 안에서만 빠진 (날짜 x 광고유형)을 0으로 채운다. 구간 밖(시작 전/중지 후)은
    공백으로 남긴다. 단 그 날짜가 시트에 통째로 없으면(수집 실패) 구간 안이어도
    공백을 유지한다 — 수집 실패를 0원으로 둔갑시키지 않기 위해.

    광고유형 값은 하드코딩하지 않고 시트에 등장한 값을 그대로 쓴다(나중에 유형이
    늘어나면 시트만 고쳐도 화면에 반영되도록).
    """
    fetch_fn = fetch_fn or read_sheet_rows_by_cols
    rows = fetch_fn(MOBWITH_B_SHEET, MOBWITH_B_COLS)

    dates_in_sheet = set()
    ad_types = []
    by_id = {}
    for r in rows:
        d = normalize_date(r.get('date'))
        pid_raw = parse_gviz_value(r.get('id'))
        imp = _num_or_none(r.get('impressions'))
        clk = _num_or_none(r.get('clicks'))
        rev = _num_or_none(r.get('revenue'))
        if not d or pid_raw is None or imp is None or clk is None or rev is None:
            continue
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        at_raw = r.get('adtype')
        at = at_raw.strip() if isinstance(at_raw, str) and at_raw.strip() else '미분류'
        if at not in ad_types:
            ad_types.append(at)

        name_raw = r.get('name')
        name = clean_placement_name(name_raw) if isinstance(name_raw, str) else str(pid)
        os_raw = r.get('os')
        os_val = os_raw if isinstance(os_raw, str) and os_raw.strip() else None

        dates_in_sheet.add(d)
        e = by_id.setdefault(pid, {'id': pid, 'name': name, 'os': os_val, 'cells': {}})
        e['cells'][(d, at)] = (imp, clk, rev)  # 중복은 마지막 값으로 덮어씀
        e['name'] = name
        if os_val:
            e['os'] = os_val

    if not dates_in_sheet:
        return {'dates': [], 'adTypes': [], 'placements': [], 'generatedAt': now_str()}

    dates = build_date_range(min(dates_in_sheet), max(dates_in_sheet))
    latest = dates[-1]

    placements = []
    for pid, e in by_id.items():
        own_dates = sorted({d for (d, _at) in e['cells']})
        first_d, last_d = own_dates[0], own_dates[-1]

        by_type = {}
        for at in ad_types:
            imps, clks, revs = [], [], []
            for d in dates:
                cell = e['cells'].get((d, at))
                if cell is not None:
                    imps.append(cell[0]); clks.append(cell[1]); revs.append(cell[2])
                elif d in dates_in_sheet and first_d <= d <= last_d:
                    imps.append(0.0); clks.append(0.0); revs.append(0.0)  # 안 적은 0
                else:
                    imps.append(None); clks.append(None); revs.append(None)
            by_type[at] = {'impressions': imps, 'clicks': clks, 'revenue': revs}

        placements.append({
            'id': pid, 'name': e['name'], 'os': e['os'],
            'byType': by_type,
            'lastDate': last_d,
            # 최신 날짜에 row가 없으면 중지(또는 일시정지)된 것으로 본다
            'stopped': last_d < latest,
        })

    def total_rev(p):
        s = 0.0
        for at in ad_types:
            s += sum(v for v in p['byType'][at]['revenue'] if v is not None)
        return s
    placements.sort(key=lambda p: -total_rev(p))

    return {'dates': dates, 'adTypes': ad_types, 'placements': placements,
            'generatedAt': now_str()}


def reconcile_mobwith(daily, b_payload):
    """B를 광고유형별로 날짜 합산한 값이 A(SSP)/C(직광고)와 맞는지 대조한다.

    수기로 수치를 옮긴 구조라 어긋날 수 있는데, 이런 불일치는 나중에 발견하면
    원인을 찾기가 매우 어렵다. 그래서 갱신할 때마다 자동으로 확인하고 차이나는
    날짜를 payload에 남겨 화면에 경고로 띄운다. 반올림 수준(RECONCILE_TOLERANCE)
    차이는 무시한다.
    """
    date_idx = {d: i for i, d in enumerate(daily['dates'])}
    # 화면 토글 키(ssp/direct)와 시트의 광고유형 값을 잇는다
    pairs = [('ssp', 'SSP'), ('direct', '직광고')]

    issues = []
    for series_key, at in pairs:
        if at not in b_payload.get('adTypes', []):
            continue
        sheet_rev = daily['series'][series_key]['revenue']
        for i, d in enumerate(b_payload['dates']):
            if d not in date_idx:
                continue
            b_sum = 0.0
            any_val = False
            for p in b_payload['placements']:
                v = p['byType'][at]['revenue'][i]
                if v is not None:
                    b_sum += v; any_val = True
            expected = sheet_rev[date_idx[d]]
            if not any_val or expected is None:
                continue
            diff = b_sum - expected
            if abs(diff) > RECONCILE_TOLERANCE:
                issues.append({'date': d, 'type': at,
                               'sheet': round(expected, 2), 'heatmap': round(b_sum, 2),
                               'diff': round(diff, 2)})
    issues.sort(key=lambda x: (x['date'], x['type']))
    return issues


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


def build_payload(fetch_sheet_fn=read_vendor_series_for_vendor, fetch_fx_fn=fetch_usd_krw_rates):

    raw_by_vendor, all_dates = {}, set()
    for key, cfg in VENDOR_CONFIG.items():
        by_date = fetch_sheet_fn(key, cfg)
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
            'group': cfg['group'],
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


APCORN_RAW_SHEET = 'APCORN_SSP_raw'
APCORN_RAW_COLS = {
    'date': 'report_date', 'pid': 'placement_id',
    'impressions': 'impression_value', 'clicks': 'click_value', 'cost': 'media_cost',
}
APCORN_MAP_SHEET = 'APCORN_SSP_ID'
APCORN_MAP_COLS = {'pid': 'Placement iD', 'name': '내용', 'os': 'OS'}


def build_apcorn_ssp_payload(fetch_fn=None, fetch_fx_fn=None):
    """APCORN SSP 지면별 일자 매트릭스.

    raw 시트는 같은 (날짜, placement_id)가 report_type(광고 종류)별로 여러 행에
    걸쳐 있으므로 전부 합산한다. request_value/response_value는 쓰지 않는다
    (요청사항). report_type=1 행은 impression/click/media_cost가 모두 0이라
    합산해도 영향이 없다.

    media_cost는 USD라 통합 매출 화면과 동일하게 당일 환율로 원화 환산한다.

    지면명·OS는 별도 매핑 시트(APCORN_SSP_ID)에서 placement_id로 찾아 붙인다.
    매핑에 없는 지면도 절대 버리지 않고 '(미등록) <id>' 이름과 os='미지정'으로
    그대로 집계에 포함한다 — 매핑을 깜빡해도 매출이 조용히 사라지지 않도록.
    """
    fetch_fn = fetch_fn or read_sheet_rows_by_cols
    fetch_fx_fn = fetch_fx_fn or fetch_usd_krw_rates

    name_by_pid, os_by_pid = {}, {}
    for r in fetch_fn(APCORN_MAP_SHEET, APCORN_MAP_COLS):
        pid = r.get('pid')
        pid = str(pid).strip() if pid is not None else ''
        if not pid:
            continue
        nm = r.get('name')
        os_v = r.get('os')
        if isinstance(nm, str) and nm.strip():
            name_by_pid[pid] = nm.strip()
        if isinstance(os_v, str) and os_v.strip():
            os_by_pid[pid] = os_v.strip()

    dates_set = set()
    by_pid = {}
    for r in fetch_fn(APCORN_RAW_SHEET, APCORN_RAW_COLS):
        d = normalize_date(r.get('date'))
        pid_raw = parse_gviz_value(r.get('pid'))
        if not d or pid_raw is None:
            continue
        pid = str(pid_raw).strip()
        if not pid:
            continue
        imp = _num_or_none(r.get('impressions')) or 0.0
        clk = _num_or_none(r.get('clicks')) or 0.0
        cost = _num_or_none(r.get('cost')) or 0.0

        dates_set.add(d)
        e = by_pid.setdefault(pid, {})
        prev = e.get(d)
        if prev:  # 같은 날짜의 다른 report_type 행 → 합산
            prev[0] += imp; prev[1] += clk; prev[2] += cost
        else:
            e[d] = [imp, clk, cost]

    if not dates_set:
        return {'dates': [], 'placements': [], 'unmappedIds': [],
                'fxAvailable': False, 'generatedAt': now_str()}

    dates = build_date_range(min(dates_set), max(dates_set))
    raw_rates = fetch_fx_fn(dates[0], dates[-1])
    fx_available = bool(raw_rates)
    rate_by_date = fill_rate_series(dates, raw_rates) if fx_available else {}

    placements, unmapped = [], []
    for pid, daily in by_pid.items():
        mapped = pid in name_by_pid
        if not mapped:
            unmapped.append(pid)
        imps, clks, revs = [], [], []
        for d in dates:
            v = daily.get(d)
            if v is None:
                imps.append(None); clks.append(None); revs.append(None); continue
            imps.append(v[0]); clks.append(v[1])
            rate = rate_by_date.get(d) if fx_available else None
            revs.append(v[2] * rate if rate else (None if fx_available else v[2]))
        placements.append({
            'id': pid,
            'name': name_by_pid.get(pid, '(미등록) ' + pid),
            'os': os_by_pid.get(pid, '미지정'),
            'mapped': mapped,
            'impressions': imps, 'clicks': clks, 'revenue': revs,
        })
    placements.sort(key=lambda p: -sum(v for v in p['revenue'] if v is not None))

    return {'dates': dates, 'placements': placements, 'unmappedIds': sorted(unmapped),
            'fxAvailable': fx_available, 'generatedAt': now_str()}


if __name__ == '__main__':
    payload = build_payload()
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    print('wrote data.json: %d dates, %d vendors' % (
        len(payload.get('dates', [])), len(payload.get('vendors', {}))))

    mobwith_a = build_mobwith_daily_payload()
    mobwith_b = build_mobwith_b_payload()

    # B(광고유형별 합계) vs A/C(시트 값) 대조 — 어긋나면 화면에 경고로 띄운다
    issues = reconcile_mobwith(mobwith_a, mobwith_b)
    mobwith_a['reconcile'] = issues
    mobwith_b['reconcile'] = issues

    with open('mobwith-a.json', 'w', encoding='utf-8') as f:
        json.dump(mobwith_a, f, ensure_ascii=False)
    print('wrote mobwith-a.json: %d dates (SSP/직광고/전체)' % len(mobwith_a.get('dates', [])))

    with open('mobwith-b.json', 'w', encoding='utf-8') as f:
        json.dump(mobwith_b, f, ensure_ascii=False)
    stopped_n = sum(1 for p in mobwith_b.get('placements', []) if p.get('stopped'))
    print('wrote mobwith-b.json: %d placements, 광고유형 %s, 중지 %d개' % (
        len(mobwith_b.get('placements', [])), mobwith_b.get('adTypes', []), stopped_n))
    if issues:
        print('  ! A/C와 B 합계가 어긋나는 날 %d건 (예: %s)' % (
            len(issues), issues[0]))

    apcorn = build_apcorn_ssp_payload()
    with open('apcorn-ssp.json', 'w', encoding='utf-8') as f:
        json.dump(apcorn, f, ensure_ascii=False)
    unmapped_n = len(apcorn.get('unmappedIds', []))
    print('wrote apcorn-ssp.json: %d placements (%d unmapped)' % (
        len(apcorn.get('placements', [])), unmapped_n))
    if unmapped_n:
        print('  ! 매핑 안 된 placement_id: %s' % ', '.join(apcorn['unmappedIds'][:10]))
