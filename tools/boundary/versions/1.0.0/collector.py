"""Authenticated, sequential BI collection using the site's own request module."""
from __future__ import annotations

import argparse
import calendar
import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PAGE_URL = 'https://aokesi.ecbis.cn/workTable/productDevelopment'
DEFAULT_PROFILE = Path('D:/ChromeDebug/User Data')
DEFAULT_PORT = 9351
ALLOWED_ENDPOINTS = {
    ('GET', '/category/categoryFirstList'),
    ('GET', '/category/getCategoryById'),
    ('GET', '/demand/getKeywordAnalysisTime'),
    ('GET', '/demand/rootKeyList'),
    ('POST', '/demand/getKeyWordByRoot'),
}


class CollectionError(ValueError):
    pass


class LoginRequired(CollectionError):
    pass


class Stopped(CollectionError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def validate_categories(rows):
    if not isinstance(rows, list):
        raise CollectionError('类目接口结构发生变化，未更新缓存。')
    result, seen = [], set()
    for item in rows:
        if not isinstance(item, dict):
            raise CollectionError('类目格式无效。')
        identifier, name = str(item.get('id', '')), str(item.get('name', '')).strip()
        if not re.fullmatch(r'[0-9]+', identifier) or not name or identifier in seen:
            raise CollectionError('类目包含无效或重复项目，未更新缓存。')
        seen.add(identifier)
        result.append({**item, 'id': identifier, 'name': name})
    return result


def needs_analysis_navigation(url):
    current=urlsplit(str(url))
    return (current.hostname=='aokesi.ecbis.cn' and current.path.startswith('/workTable')
            and current.path.rstrip('/')!='/workTable/productDevelopment')


def group_category_tree(rows,root_id):
    """getCategoryById returns a flat subtree, including the root itself."""
    rows=validate_categories(rows)
    indexed={row['id']:row for row in rows}
    groups={str(root_id):[]}
    for row in rows:
        identifier=row['id']
        groups.setdefault(identifier,[])
        if identifier==str(root_id):
            continue
        parent=str(row.get('pid',''))
        if not re.fullmatch(r'[0-9]+',parent):
            raise CollectionError('类目缺少有效上级关系，未更新缓存。')
        current,seen=identifier,set()
        while current!=str(root_id):
            if current in seen or current not in indexed:
                raise CollectionError('类目树包含循环或不属于所选一级类目，未更新缓存。')
            seen.add(current)
            current=str(indexed[current].get('pid',''))
        groups.setdefault(parent,[]).append(row)
    return groups


@dataclass(frozen=True)
class Selection:
    category_id: str
    category_name: str
    first_category_id: str
    period: str = '7'
    selected_date: str = ''

    def validate(self):
        if not all(re.fullmatch(r'[0-9]+', str(v)) for v in (self.category_id, self.first_category_id)):
            raise CollectionError('请选择有效类目。')
        if not self.category_name.strip() or self.period not in {'7', '14', '30', 'week', 'month'}:
            raise CollectionError('类目或时间类型无效。')
        if self.period == 'week':
            date.fromisoformat(self.selected_date)
        elif self.period == 'month':
            if not re.fullmatch(r'\d{4}-\d{2}', self.selected_date):
                raise CollectionError('月份请填写 YYYY-MM。')
            date.fromisoformat(self.selected_date + '-01')
        return self


def period_params(selection, limits):
    """Match useDateFormat: rolling roots use N; keywords use maxDayDate."""
    selection.validate()
    if selection.period.isdigit():
        window = int(selection.period)
        end = date.fromisoformat(limits['maxDayDate'])
        start = end - timedelta(days=window - 1)
        if start < date.fromisoformat(limits['minDayDate']):
            raise CollectionError('该类目的可用日期不足所选天数。')
        return {'root_date': window, 'keyword_date': end.strftime('%Y%m%d'),
                'dates_type': window, 'start': start.isoformat(), 'end': end.isoformat()}
    if selection.period == 'week':
        chosen = date.fromisoformat(selection.selected_date)
        # Site uses dayjs startOf/endOf('week'): Sunday through Saturday.
        end = chosen + timedelta(days=(5 - chosen.weekday()) % 7)
        start = end - timedelta(days=6)
        if not date.fromisoformat(limits['minWeekDate']) <= end <= date.fromisoformat(limits['maxWeekDate']):
            raise CollectionError('所选周超出该类目的可用周范围。')
        value = end.strftime('%Y%m%d')
    else:
        start = date.fromisoformat(selection.selected_date + '-01')
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        value = start.strftime('%Y%m')
        if not str(limits['minMonthDate']) <= value <= str(limits['maxMonthDate']):
            raise CollectionError('所选月超出该类目的可用月份范围。')
    return {'root_date': value, 'keyword_date': value, 'dates_type': selection.period,
            'start': start.isoformat(), 'end': end.isoformat()}


def parse_bindings(demand_source, shared_source):
    """Resolve current export names without freezing hashed filenames or RSA keys."""
    imp = re.search(r'import\{([^}]+)\}from["\']\./(shared-[\w-]+\.js)["\']', demand_source)
    if not imp:
        raise CollectionError('网站请求模块结构发生变化，需要更新脚本。')
    aliases = {}
    for exported, local in re.findall(r'([\w$]+)\s+as\s+([\w$]+)', imp[1]):
        aliases[local] = exported
    result = {'shared_filename': imp[2]}
    for name, endpoint in [('get', '/demand/rootKeyList'), ('post', '/demand/getKeyWordByRoot')]:
        match = re.search(r'([\w$]+)\(`' + re.escape(endpoint) + r'`', demand_source)
        if not match or match[1] not in aliases:
            raise CollectionError('网站请求方法发生变化，需要更新脚本。')
        result[name] = aliases[match[1]]
    store = re.search(r'(?:var |,)([\w$]+)=[\w$]+\(`wasm`,\{', shared_source)
    exports = shared_source[shared_source.rfind('export{'):]
    exported = re.search(r'(?:\{|,)' + re.escape(store[1]) + r' as ([\w$]+)(?:,|\})', exports) if store else None
    if not exported:
        raise CollectionError('网站数据解码模块发生变化，需要更新脚本。')
    result['crypto'] = exported[1]
    return result


BRIDGE = r'''
const cfg=arguments[0], endpoint=arguments[1], params=arguments[2], method=arguments[3];
return (async()=>{
  const timer=new Promise((_,reject)=>setTimeout(()=>reject(new Error('request timeout')),50000));
  const execute=async()=>{
    const module=await import(cfg.shared_url);
    const response=await module[cfg[method]](endpoint,params,{timeout:45000,isMessage:false});
    const body=response && response.data;
    if (!body || typeof body!=='object') return {status:response?.status||0,invalid:true};
    if (body.success!==true || body.code!==0) return {status:response.status,success:body.success,code:body.code};
    let data=body.data;
    if (endpoint==='/demand/rootKeyList' || endpoint==='/demand/getKeyWordByRoot'){
      if(typeof data==='string'){
        const crypto=module[cfg.crypto]();
        await crypto.initialize();
        if (!crypto.isReady) throw new Error('decoder unavailable');
        data=crypto.decryptByAES(data,false);
      }
    }
    return {status:response.status,success:body.success,code:body.code,data};
  };
  try{return await Promise.race([execute(),timer]);}
  catch(e){return {failed:true,status:e?.response?.status||0,code:e?.response?.data?.code||null};}
})();
'''


def checked_response(result):
    if not isinstance(result, dict):
        raise CollectionError('页面未返回有效数据。')
    status, code = result.get('status'), str(result.get('code', ''))
    if status == 401 or code == '401':
        raise LoginRequired('登录已失效。请在打开的浏览器中完成登录，再重新采集。')
    if status == 429 or code == '429':
        raise CollectionError('接口提示请求过于频繁，已停止。请稍后重试。')
    if status == 403 or code == '403':
        raise CollectionError('接口拒绝访问，请检查账号权限或浏览器中的验证提示。')
    if result.get('failed') or status != 200 or result.get('success') is not True or code != '0':
        raise CollectionError(f'接口未成功（HTTP {status or "未知"}，业务码 {code or "未知"}），没有保存为空数据。')
    return result.get('data')


class BIClient:
    def __init__(self, profile=DEFAULT_PROFILE, port=DEFAULT_PORT, log=print, stop=None):
        self.profile = Path(profile).expanduser().resolve()
        self.port, self.log = int(port), log
        if not 1024 <= self.port <= 65535:
            raise CollectionError('浏览器端口须在 1024–65535 之间。')
        self.stop = stop or threading.Event()
        self.browser = self.tab = self.bindings = None

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped('已停止。已完成的 JSON 数据仍保留在输出目录。')

    def verify_port(self):
        with socket.socket() as s:
            occupied = s.connect_ex(('127.0.0.1', self.port)) == 0
        if not occupied:
            return
        import psutil
        expected = str(self.profile).casefold()
        for conn in psutil.net_connections(kind='tcp'):
            if conn.status == 'LISTEN' and conn.laddr.port == self.port and conn.pid:
                cmd = psutil.Process(conn.pid).cmdline()
                values = [a.split('=', 1)[1] for a in cmd if a.startswith('--user-data-dir=')]
                if values and str(Path(values[0]).resolve()).casefold() == expected:
                    return
        raise CollectionError('该端口已被其他程序或其他浏览器资料占用，请更换端口。')

    def connect(self, login_timeout=300):
        from DrissionPage import Chromium, ChromiumOptions
        self.check_stop()
        self.verify_port()
        options = ChromiumOptions(read_file=False)
        options.set_local_port(self.port)
        options.set_user_data_path(str(self.profile))
        options.set_argument('--profile-directory=Default')
        options.set_argument('--start-maximized')
        options.set_argument('--no-first-run')
        options.set_argument('--no-default-browser-check')
        for candidate in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
                          Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe')]:
            if candidate.is_file():
                options.set_browser_path(str(candidate))
                break
        try:
            self.browser = Chromium(options)
            tabs = [t for t in self.browser.get_tabs(url='aokesi.ecbis.cn')
                    if urlsplit(str(t.url)).hostname == 'aokesi.ecbis.cn']
            self.tab = tabs[0] if tabs else self.browser.new_tab()
            self.tab.set.window.max()
            self.tab.get(PAGE_URL, timeout=30)
            self.tab.set.activate()
        except Exception as exc:
            raise CollectionError('无法打开 Chrome。请检查浏览器资料是否被占用，或更换资料目录和端口。') from exc
        self.log('已打开边界BI，正在等待登录。首次使用请在浏览器中正常登录。')
        deadline = time.monotonic() + login_timeout
        navigated=set()
        while time.monotonic() < deadline:
            self.check_stop()
            try:
                if urlsplit(str(self.tab.url)).hostname == 'aokesi.ecbis.cn':
                    current=str(self.tab.url)
                    if needs_analysis_navigation(current) and current not in navigated:
                        navigated.add(current)
                        self.log('登录后已进入工作台，正在打开产品需求分析。')
                        self.tab.get(PAGE_URL,timeout=30)
                        continue
                    ready = self.tab.run_js("return document.body?.innerText.includes('词根分析') && !location.pathname.includes('login');", timeout=5)
                    if ready:
                        self.read_cookies()
                        self.discover()
                        self.log('页面会话已就绪，已自动读取 Cookie。')
                        return self
            except CollectionError:
                raise
            except Exception:
                pass
            self.stop.wait(1)
        raise LoginRequired('等待登录超时。完成登录后再点击采集，浏览器会保留。')

    def read_cookies(self):
        # Only inspect this site's cookies. Requests remain in Chrome, which applies
        # domain/path/SameSite/HttpOnly rules itself. Never export credentials.
        cookies = self.tab.cookies(all_domains=True, all_info=True)
        count = sum(1 for c in cookies if str(c.get('domain', '')).lstrip('.') == 'ecbis.cn'
                    or str(c.get('domain', '')).lstrip('.').endswith('.ecbis.cn'))
        if not count:
            raise LoginRequired('尚未获取到边界BI登录状态，请先在浏览器中登录。')

    def discover(self):
        import requests
        urls = self.tab.run_js("return performance.getEntriesByType('resource').map(x=>x.name);", timeout=10)
        demand = [u for u in (urls or []) if re.fullmatch(r'https://aokesi\.ecbis\.cn/static/js/demandInsightApi-[\w-]+\.js', u)]
        if not demand:
            raise CollectionError('未加载词根分析模块。请在浏览器进入产品需求分析页面再试。')
        # These are public static assets, no Cookie is copied into this session.
        with requests.Session() as session:
            r = session.get(demand[-1], timeout=30)
            r.raise_for_status()
            imp = re.search(r'from["\']\./(shared-[\w-]+\.js)["\']', r.text)
            if not imp:
                raise CollectionError('未找到当前版本的请求模块。')
            shared_url = 'https://aokesi.ecbis.cn/static/js/' + imp[1]
            shared = session.get(shared_url, timeout=30)
            shared.raise_for_status()
        self.bindings = parse_bindings(r.text, shared.text)
        self.bindings['shared_url'] = shared_url

    def request(self, endpoint, params, method='GET'):
        self.check_stop()
        if (method, endpoint) not in ALLOWED_ENDPOINTS:
            raise CollectionError('不支持的请求。')
        self.read_cookies()
        if not self.bindings:
            raise CollectionError('请先打开浏览器并完成登录。')
        try:
            result = self.tab.run_js(BRIDGE, self.bindings, endpoint, params, method.lower(), timeout=60)
        except Exception as exc:
            raise CollectionError('浏览器请求超时或连接已断开，请检查页面后重新采集。') from exc
        self.check_stop()
        return checked_response(result)

    def categories(self, parent=None):
        args = {'platform': 'TX'}
        if parent:
            args.update(categoryId=str(parent), isAuth=True)
        return validate_categories(self.request('/category/getCategoryById' if parent else '/category/categoryFirstList', args))

    def limits(self, category_id):
        result = self.request('/demand/getKeywordAnalysisTime',
                              {'platform': 'TX', 'categoryId': str(category_id), 'pluginType': 'productDevelopment'})
        required = {'minDayDate','maxDayDate','minWeekDate','maxWeekDate','minMonthDate','maxMonthDate'}
        if not isinstance(result, dict) or not required.issubset(result):
            raise CollectionError('该类目未返回完整可用时间范围。')
        return result

    def collect(self, selection, output_dir=None):
        from excel_export import export_result
        selection.validate()
        output_dir = Path(output_dir or ROOT / 'outputs')
        self.log('正在查询该类目的可用日期。')
        limits = self.limits(selection.category_id)
        period = period_params(selection, limits)
        params = {'platform':'TX', 'cateId':selection.category_id, 'date':period['root_date'],
                  'datesType':period['dates_type'], 'useGpt':0, 'firstCateId':selection.first_category_id}
        self.log(f"正在获取词根：{selection.category_name}，{period['start']} 至 {period['end']}。")
        roots = self.request('/demand/rootKeyList', params)
        if not isinstance(roots, dict) or not isinstance(roots.get('keyWordVoList'), list):
            raise CollectionError('词根响应结构异常，没有导出空表。')
        actual_start, actual_end = roots.get('minMonth'), roots.get('maxMonth')
        if (actual_start, actual_end) != (period['start'], period['end']):
            raise CollectionError(f'服务端返回周期 {actual_start} 至 {actual_end} 与选择不一致，未继续采集。')
        stamp = datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', selection.category_name)[:55]
        basename = f"{safe}_{selection.category_id}_{selection.period}_{period['end']}_{stamp}"
        raw_path = output_dir / (basename + '.json')
        result = {'selection':selection.__dict__, 'period':period, 'available_time':limits,
                  'fetched_at':datetime.now().astimezone().isoformat(timespec='seconds'),
                  'source':'https://biplugs.ecbis.cn/prod/demand', 'roots':roots,
                  'status':'partial', 'keywords':None}
        atomic_json(raw_path, result)
        self.stop.wait(1)
        self.check_stop()
        use_ai = roots.get('useAi')
        if not isinstance(use_ai, bool):
            raise CollectionError('词根响应未返回 useAi 状态，已保留词根 JSON，请检查网站接口。')
        body = {'platform':'TX','cateId':selection.category_id,'date':period['keyword_date'],
                'datesType':period['dates_type'],'rootWordList':[], 'useAi':use_ai}
        self.log('正在获取完整关键词响应。')
        keywords = self.request('/demand/getKeyWordByRoot', body, 'POST')
        if not isinstance(keywords, dict) or not isinstance(keywords.get('hotWordList'), list):
            raise CollectionError('关键词响应结构异常，词根 JSON 已保留。')
        result.update(keywords=keywords, status='complete')
        atomic_json(raw_path, result)
        self.check_stop()
        output = raw_path.with_suffix('.xlsx')
        export_result(result, output)
        self.log(f"完成：{len(roots['keyWordVoList'])} 个词根、{len(keywords['hotWordList'])} 条关键词。")
        if len(keywords['hotWordList']) >= 2000:
            self.log('关键词达到 2000 条，可能存在服务端返回上限，Excel 中已注明。')
        return output


def main():
    parser = argparse.ArgumentParser(description='边界BI词根分析采集')
    parser.add_argument('--category-id', required=True)
    parser.add_argument('--category-name', required=True)
    parser.add_argument('--first-category-id', required=True)
    parser.add_argument('--period', choices=['7','14','30','week','month'], default='7')
    parser.add_argument('--date', default='', help='指定周中任意日期 YYYY-MM-DD，或月份 YYYY-MM')
    parser.add_argument('--profile', default=str(DEFAULT_PROFILE))
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--output-dir', default=str(ROOT/'outputs'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        selection = Selection(args.category_id,args.category_name,args.first_category_id,args.period,args.date).validate()
        if args.dry_run:
            print(json.dumps(selection.__dict__,ensure_ascii=False,indent=2))
            return 0
        client = BIClient(args.profile,args.port).connect()
        print(client.collect(selection,args.output_dir))
        return 0
    except (CollectionError, ValueError) as exc:
        print(str(exc))
        return 1
    except Exception:
        print('采集未完成，请检查浏览器、网络及依赖。没有覆盖已有结果。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
