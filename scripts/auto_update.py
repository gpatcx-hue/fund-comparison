"""
固收+基金对比报告 — 每日全自动数据更新
流水线: 天天基金API拉净值 → 计算指标 → 季报仓位 → 场景归因 → Beta分解 → fund-data.json
"""

import sys, os, json, math, time, requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ============================================================
# 配置
# ============================================================

TODAY = datetime.now().strftime('%Y-%m-%d')
CACHE_PATH = Path(__file__).parent.parent / 'nav_cache.json'
CONFIG_PATH = Path(__file__).parent.parent / 'funds.json'
OUTPUT_PATH = Path(__file__).parent.parent / 'fund-data.json'

THREADS = 5
SLEEP = 0.3
EASTMONEY_NAV_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList'
EASTMONEY_INFO_URL = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo'
EASTMONEY_ALLOC_URL = 'https://fundf10.eastmoney.com/FundArchivesDatas.aspx'
RISK_FREE_RATE = 0.011  # 1年期存款利率，备用值

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
    'Referer': 'https://app.cntech.com.cn/',
    'Content-Type': 'application/x-www-form-urlencoded',
}


# ============================================================
# 1. 读取配置
# ============================================================

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================
# 2. 拉取净值数据 (复用 fund-platform 的天天基金API)
# ============================================================

def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, 'r') as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f)


def fetch_nav(code, cache):
    """从天天基金拉取单只基金净值序列"""
    cached = cache.get(code, [])
    page_size = 10 if cached else 40000

    params = {
        'FCODE': code,
        'deviceid': 'pipeline',
        'plat': 'Iphone',
        'product': 'EFund',
        'version': '2.0',
        'pageIndex': 1,
        'pageSize': page_size,
        'appType': 'ttjj',
    }

    try:
        resp = requests.get(EASTMONEY_NAV_URL, params=params, headers=HEADERS, timeout=30)
        data = resp.json()
        records = data.get('Datas', [])

        if not records:
            print(f"  [WARN] {code}: no NAV data returned")
            return cached

        # Parse records: each has FSRQ (date), DWJZ (unit NAV), LJJZ (cumulative NAV)
        new_entries = []
        for r in records:
            try:
                entry = {
                    'date': r['FSRQ'],
                    'nav': float(r['DWJZ']),
                    'acc_nav': float(r['LJJZ']),
                }
                new_entries.append(entry)
            except (KeyError, ValueError):
                continue

        if not new_entries:
            return cached

        # Merge with cache
        if cached:
            existing_dates = {e['date'] for e in cached}
            merged = cached + [e for e in new_entries if e['date'] not in existing_dates]
            merged.sort(key=lambda x: x['date'])
            return merged
        else:
            new_entries.sort(key=lambda x: x['date'])
            return new_entries

    except Exception as e:
        print(f"  [ERR] {code}: {e}")
        return cached


def fetch_all_navs(codes, cache):
    """多线程并发拉取所有基金净值"""
    results = {}

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(fetch_nav, code, cache): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as e:
                print(f"  [ERR] {code}: {e}")
                results[code] = cache.get(code, [])
            time.sleep(SLEEP)

    return results


# ============================================================
# 3. 计算收益和风险指标
# ============================================================

def build_adjusted_nav(nav_list):
    """从单位净值+累计净值重建复权净值"""
    if not nav_list:
        return []

    adj = [0.0] * len(nav_list)
    adj[-1] = nav_list[-1]['nav']

    for i in range(len(nav_list) - 2, -1, -1):
        nav_t = nav_list[i]['nav']
        nav_t1 = nav_list[i + 1]['nav']
        acc_t = nav_list[i]['acc_nav']
        acc_t1 = nav_list[i + 1]['acc_nav']

        dividend = (acc_t1 - acc_t) - (nav_t1 - nav_t)
        if dividend > 0.001:
            adj[i] = adj[i + 1] / (1 + dividend / nav_t)
        else:
            adj[i] = adj[i + 1] * (nav_t / nav_t1) if nav_t1 > 0 else adj[i + 1]

    return adj


def calc_period_return(adj_navs, dates, target_date_str, latest_idx):
    """计算指定日期到最新日期的收益率"""
    target = datetime.strptime(target_date_str, '%Y-%m-%d')

    # Find nearest trading day >= target
    best_idx = None
    for i, d in enumerate(dates):
        dt = datetime.strptime(d, '%Y-%m-%d')
        if dt >= target:
            best_idx = i
            break

    if best_idx is None or best_idx >= latest_idx:
        return None
    if adj_navs[best_idx] <= 0:
        return None

    ret = (adj_navs[latest_idx] / adj_navs[best_idx] - 1) * 100
    return round(ret, 2)


def calc_metrics(nav_list, rf=RISK_FREE_RATE):
    """计算完整的收益和风险指标"""
    if len(nav_list) < 30:
        return None

    adj = build_adjusted_nav(nav_list)
    dates = [e['date'] for e in nav_list]
    latest = len(adj) - 1

    if adj[latest] <= 0:
        return None

    latest_date = dates[latest]

    # Period returns
    now = datetime.strptime(latest_date, '%Y-%m-%d')
    periods = {
        'ytd': now.replace(month=1, day=1).strftime('%Y-%m-%d'),
        'm1': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
        'm3': (now - timedelta(days=91)).strftime('%Y-%m-%d'),
        'm6': (now - timedelta(days=182)).strftime('%Y-%m-%d'),
        'y1': (now - timedelta(days=365)).strftime('%Y-%m-%d'),
        'y2': (now - timedelta(days=730)).strftime('%Y-%m-%d'),
        'y3': (now - timedelta(days=1095)).strftime('%Y-%m-%d'),
    }

    returns = {}
    for key, start_date in periods.items():
        r = calc_period_return(adj, dates, start_date, latest)
        if r is not None:
            # Guard against obviously wrong returns (>200% for any period is anomalous for 固收+)
            if abs(r) > 200:
                print('  [WARN] Anomalous return for {}: {} = {}% (possible data issue)'.format(
                    dates[latest] if dates else '?', key, r))
                r = None
        returns[key] = r if r is not None else ''

    # Daily returns for vol calculation (1Y window)
    y1_idx = None
    target_1y = (now - timedelta(days=365)).strftime('%Y-%m-%d')
    for i, d in enumerate(dates):
        if d >= target_1y:
            y1_idx = i
            break
    if y1_idx is None:
        y1_idx = max(0, latest - 250)

    daily_rets = []
    for i in range(y1_idx + 1, latest + 1):
        if adj[i - 1] > 0:
            daily_rets.append(adj[i] / adj[i - 1] - 1)

    # Annualized return — compute directly from adjusted NAV, not from returns dict
    # This avoids the '' or 0 trap when returns['y1'] is unavailable
    if adj[y1_idx] > 0 and adj[latest] > 0:
        actual_days = latest - y1_idx
        ann_ret = (adj[latest] / adj[y1_idx]) ** (365.0 / max(actual_days, 1)) - 1
    else:
        ann_ret = 0

    vol = 0
    if len(daily_rets) > 20:
        mean_r = sum(daily_rets) / len(daily_rets)
        var = sum((r - mean_r) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
        vol = math.sqrt(var * 250) * 100

    # Max drawdown (1Y)
    max_dd = 0
    peak = adj[y1_idx]
    for i in range(y1_idx, latest + 1):
        if adj[i] > peak:
            peak = adj[i]
        dd = (adj[i] / peak - 1) * 100 if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # Sharpe ratio — all values in decimal form
    vol_dec = vol / 100
    sharpe = round((ann_ret - rf) / vol_dec, 2) if vol_dec > 0 else ''

    # Calmar ratio — ann_ret is decimal, max_dd is percentage
    calmar = round(ann_ret * 100 / abs(max_dd), 2) if max_dd != 0 else ''

    # Sortino ratio
    neg_rets = [r for r in daily_rets if r < 0]
    if len(neg_rets) > 10:
        down_var = sum(r ** 2 for r in neg_rets) / len(neg_rets)
        down_vol_dec = math.sqrt(down_var * 250)
        sortino = round((ann_ret - rf) / down_vol_dec, 2) if down_vol_dec > 0 else ''
    else:
        sortino = ''

    # 90-day NAV history for sparkline
    history_90 = [round(adj[i], 4) for i in range(max(0, latest - 89), latest + 1)]

    return {
        'nav': round(adj[latest], 4),
        'navDate': latest_date,
        'returns': returns,
        'risk': {
            'annualVol': round(vol, 2) if vol else '',
            'maxDrawdown': round(max_dd, 2) if max_dd else '',
            'sharpe': sharpe,
            'calmar': calmar,
            'sortino': sortino,
        },
        'history': history_90,
    }


# ============================================================
# 4. 场景收益计算
# ============================================================

def calc_scenario_returns(nav_list, scenarios):
    """计算各历史场景的区间收益"""
    if not nav_list:
        return {}

    adj = build_adjusted_nav(nav_list)
    dates = [e['date'] for e in nav_list]
    result = {}

    for key, scenario in scenarios.items():
        start_str = scenario['start']
        end_str = scenario['end']

        # Find start index
        start_idx = None
        for i, d in enumerate(dates):
            if d >= start_str:
                start_idx = i
                break

        # Find end index
        end_idx = None
        for i, d in enumerate(dates):
            if d >= end_str:
                end_idx = i
                break
        if end_idx is None:
            end_idx = len(dates) - 1

        if start_idx is not None and end_idx > start_idx and adj[start_idx] > 0:
            ret = (adj[end_idx] / adj[start_idx] - 1) * 100
            result[key] = round(ret, 2)
        else:
            result[key] = ''

    return result


# ============================================================
# 5. Beta 分解
# ============================================================

def calc_beta_decomp(nav_list, benchmark_navs):
    """简单 OLS 回归: fund_return = alpha + beta_eq * eq_return + beta_bd * bd_return"""
    if not nav_list:
        return {}

    adj = build_adjusted_nav(nav_list)
    dates = [e['date'] for e in nav_list]

    # Use last 1 year of daily returns
    now = datetime.strptime(dates[-1], '%Y-%m-%d')
    target = (now - timedelta(days=365)).strftime('%Y-%m-%d')

    start_idx = 0
    for i, d in enumerate(dates):
        if d >= target:
            start_idx = i
            break

    if start_idx >= len(adj) - 30:
        return {}

    # Build fund daily returns
    fund_rets = []
    ret_dates = []
    for i in range(start_idx + 1, len(adj)):
        if adj[i - 1] > 0:
            fund_rets.append(adj[i] / adj[i - 1] - 1)
            ret_dates.append(dates[i])

    if len(fund_rets) < 50:
        return {}

    # Build benchmark returns (align dates — skip days with missing benchmark data)
    def build_bench_rets(bench_list):
        if not bench_list:
            return None  # Signal no benchmark data
        bench_adj = build_adjusted_nav(bench_list)
        bench_dates = [e['date'] for e in bench_list]
        bench_map = {d: bench_adj[i] for i, d in enumerate(bench_dates)}

        rets = []
        valid_mask = []  # Track which fund dates had valid benchmark data
        prev = None
        for d in ret_dates:
            curr = bench_map.get(d)
            if curr and prev and prev > 0:
                rets.append(curr / prev - 1)
                valid_mask.append(True)
            else:
                rets.append(None)  # Will be filtered out
                valid_mask.append(False)
            if curr:
                prev = curr
        return rets, valid_mask

    eq_result = build_bench_rets(benchmark_navs.get('eq', []))
    bd_result = build_bench_rets(benchmark_navs.get('bd', []))

    if eq_result is None or bd_result is None:
        return {}

    eq_rets_all, eq_mask = eq_result
    bd_rets_all, bd_mask = bd_result

    # Filter: only use days where BOTH benchmarks AND fund have valid data
    y_filtered = []
    eq_filtered = []
    bd_filtered = []
    for i in range(min(len(fund_rets), len(eq_rets_all), len(bd_rets_all))):
        if eq_mask[i] and bd_mask[i] and eq_rets_all[i] is not None and bd_rets_all[i] is not None:
            y_filtered.append(fund_rets[i])
            eq_filtered.append(eq_rets_all[i])
            bd_filtered.append(bd_rets_all[i])

    # OLS: y = a + b1*x1 + b2*x2
    # Using normal equations: beta = (X'X)^{-1} X'y
    n = len(y_filtered)
    if n < 50:
        return {}

    y = y_filtered
    x1 = eq_filtered
    x2 = bd_filtered

    # Means
    my = sum(y) / n
    mx1 = sum(x1) / n
    mx2 = sum(x2) / n

    # Covariances
    s11 = sum((x1[i] - mx1) ** 2 for i in range(n))
    s22 = sum((x2[i] - mx2) ** 2 for i in range(n))
    s12 = sum((x1[i] - mx1) * (x2[i] - mx2) for i in range(n))
    sy1 = sum((y[i] - my) * (x1[i] - mx1) for i in range(n))
    sy2 = sum((y[i] - my) * (x2[i] - mx2) for i in range(n))

    # Solve 2x2 system
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-12:
        return {}

    beta_eq = (s22 * sy1 - s12 * sy2) / det
    beta_bd = (s11 * sy2 - s12 * sy1) / det
    alpha_daily = my - beta_eq * mx1 - beta_bd * mx2

    # R-squared
    ss_res = sum((y[i] - alpha_daily - beta_eq * x1[i] - beta_bd * x2[i]) ** 2 for i in range(n))
    ss_tot = sum((y[i] - my) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    # Annualize
    alpha_ann = alpha_daily * 250 * 100

    # Tracking error
    residuals = [y[i] - alpha_daily - beta_eq * x1[i] - beta_bd * x2[i] for i in range(n)]
    if len(residuals) > 1:
        te_var = sum(r ** 2 for r in residuals) / (len(residuals) - 1)
        tracking_err = math.sqrt(te_var * 250) * 100
    else:
        tracking_err = 0

    # Information ratio
    info_ratio = round(alpha_ann / tracking_err, 2) if tracking_err > 0 else ''

    return {
        'eqBeta': round(beta_eq, 3),
        'bdBeta': round(beta_bd, 3),
        'alpha': round(alpha_ann, 2),
        'rSquared': round(max(0, min(1, r2)), 3),
        'trackingErr': round(tracking_err, 2),
        'infoRatio': info_ratio,
    }


# ============================================================
# 6. 季报仓位 (简化版)
# ============================================================

def fetch_quarterly_allocation(code):
    """从天天基金获取最近几个季度的资产配置"""
    url = 'https://fundf10.eastmoney.com/zcpz_{}.html'.format(code)
    try:
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/',
        }, timeout=15)

        html = resp.text
        import re
        # Find the asset allocation table (class="tzxq")
        table_match = re.search(r'<table[^>]*class="[^"]*tzxq[^"]*"[^>]*>(.*?)</table>', html, re.DOTALL)
        if not table_match:
            print('  [WARN] {}: no tzxq table found'.format(code))
            return []

        table_html = table_match.group(1)
        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)

        quarters = []
        for row in rows:
            # Cells: 报告期, 股票占净比, 债券占净比, 现金占净比, 净资产
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) >= 4:
                period = re.sub(r'<[^>]+>', '', cells[0]).strip()
                stock = re.sub(r'<[^>]+>', '', cells[1]).strip().replace('%', '')
                bond = re.sub(r'<[^>]+>', '', cells[2]).strip().replace('%', '')
                cash = re.sub(r'<[^>]+>', '', cells[3]).strip().replace('%', '')

                if period and stock:
                    try:
                        dt = datetime.strptime(period, '%Y-%m-%d')
                        q_label = '{}Q{}'.format(str(dt.year)[2:], (dt.month - 1) // 3 + 1)
                        quarters.append({
                            'quarter': q_label,
                            'stock': stock,
                            'bond': bond,
                            'cash': cash,
                            'other': str(round(100 - float(stock or 0) - float(bond or 0) - float(cash or 0), 1)),
                        })
                    except:
                        continue

        # Return last 8 quarters in chronological order
        quarters.reverse()
        return quarters[-8:] if len(quarters) >= 8 else quarters

    except Exception as e:
        print('  [WARN] {}: allocation fetch failed: {}'.format(code, e))
        return []


# ============================================================
# 7. 主流程
# ============================================================

def main():
    print(f"=== 固收+对比数据更新 {TODAY} ===\n")

    config = load_config()
    funds = config['funds']
    scenarios = config['scenarios']
    benchmarks = config['benchmarks']

    codes = [f['code'] for f in funds]
    bench_codes = [benchmarks['eqIndex'], benchmarks['bdIndex']]
    all_codes = list(set(codes + bench_codes))

    print(f"[1/6] Loading cache...")
    cache = load_cache()

    print(f"[2/6] Fetching NAV for {len(all_codes)} funds ({THREADS} threads)...")
    nav_data = fetch_all_navs(all_codes, cache)

    # Save cache
    cache.update(nav_data)
    save_cache(cache)
    print(f"  Cache saved ({len(cache)} funds)\n")

    # Benchmark NAVs for beta decomposition
    benchmark_navs = {
        'eq': nav_data.get(benchmarks['eqIndex'], []),
        'bd': nav_data.get(benchmarks['bdIndex'], []),
    }

    print(f"[3/6] Calculating metrics...")
    fund_results = {}
    for fund in funds:
        code = fund['code']
        nav_list = nav_data.get(code, [])
        metrics = calc_metrics(nav_list)
        if metrics:
            fund_results[code] = metrics
            print(f"  {code} {fund['name']}: NAV={metrics['nav']}, 1Y={metrics['returns'].get('y1', 'N/A')}%")
        else:
            print(f"  {code} {fund['name']}: insufficient data")

    print(f"\n[4/6] Calculating scenario returns...")
    scenario_data = {}
    for fund in funds:
        code = fund['code']
        nav_list = nav_data.get(code, [])
        scenario_data[code] = calc_scenario_returns(nav_list, scenarios)

    print(f"\n[5/6] Beta decomposition...")
    beta_data = {}
    for fund in funds:
        code = fund['code']
        nav_list = nav_data.get(code, [])
        beta_data[code] = calc_beta_decomp(nav_list, benchmark_navs)
        bd = beta_data[code]
        if bd:
            print(f"  {code}: α={bd.get('alpha', 'N/A')}%, β_eq={bd.get('eqBeta', 'N/A')}, R²={bd.get('rSquared', 'N/A')}")

    print(f"\n[6/6] Fetching quarterly allocations...")
    allocation_data = {}
    for fund in funds:
        code = fund['code']
        allocation_data[code] = fetch_quarterly_allocation(code)
        if allocation_data[code]:
            print(f"  {code}: {len(allocation_data[code])} quarters loaded")
        time.sleep(SLEEP)

    # Build output
    output = {
        'updatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'funds': [],
        'scenarios': scenarios,
        'benchmarks': {
            'eqName': benchmarks['eqIndexName'],
            'bdName': benchmarks['bdIndexName'],
        },
    }

    for fund in funds:
        code = fund['code']
        m = fund_results.get(code, {})
        entry = {
            'code': code,
            'name': fund['name'],
            'company': fund.get('company', ''),
            'manager': fund.get('manager', ''),
            'nav': m.get('nav', ''),
            'navDate': m.get('navDate', ''),
            'returns': m.get('returns', {}),
            'risk': m.get('risk', {}),
            'history': m.get('history', []),
            'quarterlyHistory': allocation_data.get(code, []),
            'scenarioReturns': scenario_data.get(code, {}),
            'betaDecomp': beta_data.get(code, {}),
        }
        output['funds'].append(entry)

    # Write output
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    valid = len(fund_results)
    total = len(funds)
    print(f"\n=== Done: {valid}/{total} funds with valid data ===")
    print(f"Output: {OUTPUT_PATH}")

    if valid < total / 3:
        print("[WARN] Less than 1/3 funds have valid data, check network/API")
        sys.exit(1)


if __name__ == '__main__':
    main()
