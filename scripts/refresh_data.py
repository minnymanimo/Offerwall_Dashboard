#!/usr/bin/env python3
"""
오퍼월 광고 매출 대시보드 — 데이터 갱신 스크립트 (GitHub Actions에서 실행)

구글시트 4개 벤더 탭(NBT_Adison, Buzzvil, APCORN_SSP, ADOP)을 공개 gviz 엔드포인트로
읽어와 정리한 뒤 data.json으로 저장합니다.

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
        return v.isoformat()
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
