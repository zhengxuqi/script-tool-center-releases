"""Fetch DMP crowd coverage using an existing authorized login session."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE_URL = "https://dmp.taobao.com/api_2/analysis/coverage"
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PAGE_URL = "https://dmp.taobao.com/index_new.html#!/crowds-new/create"
_EXISTING_USER_DATA = Path(r"D:\ChromeDebug\User Data")
DEFAULT_USER_DATA = Path(os.environ.get("DMP_USER_DATA_DIR") or
                         (_EXISTING_USER_DATA if _EXISTING_USER_DATA.exists() else ROOT / "browser-profile"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def date_value(value):
    value = str(value).replace("-", "")
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError("日期应为 YYYY-MM-DD 或 YYYYMMDD")
    datetime.strptime(value, "%Y%m%d")
    return value


def build_payload(config):
    start, end = date_value(config["start_date"]), date_value(config["end_date"])
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    low, high = str(config["min_amount"]), str(config["max_amount"])
    try:
        a, b = Decimal(low), Decimal(high)
        if not a.is_finite() or not b.is_finite() or a < 0 or a > b:
            raise ValueError("金额须为非负数，且最小金额不能大于最大金额")
    except InvalidOperation as exc:
        raise ValueError("金额必须是数字") from exc
    category = str(config["category_id"])
    if not re.fullmatch(r"\d+", category):
        raise ValueError("category_id 必须是数字类目 ID")
    entries = [
        (171470, category, config.get("category_name", category)),
        (171471, "trd", "购买"),
        (171475, "amt", "购买金额"),
        (171477, f"{low}~{high}", f"{low}~{high}"),
        (171480, "2", "时间区间"),
        (171482, f"{start}~{end}", f"{start}~{end}"),
    ]
    selects = [
        {"tagName": "类目行为人群", "tagId": "224472", "optionGroupId": group,
         "values": value, "names": name, "adapterType": None}
        for group, value, name in entries
    ]
    return {"selectTagOptionSet": {"operator": 2, "selectTagOptionSet": [
        {"operator": 1, "selects": selects, "tagFrom": 1}
    ]}}


def credentials(cookie_path):
    cookie = os.environ.get("DMP_COOKIE", "").strip()
    if not cookie:
        if not cookie_path.is_file():
            raise ValueError(f"请将登录 Cookie 存入 {cookie_path}，或设置 DMP_COOKIE 环境变量")
        cookie = cookie_path.read_text(encoding="utf-8-sig").strip()
    cookie = re.sub(r"^cookie:\s*", "", cookie, flags=re.I)
    if not cookie or "\n" in cookie or "\r" in cookie:
        raise ValueError("Cookie 应为一行完整请求 Cookie，不是整个请求头")
    values = {}
    for item in cookie.split(";"):
        key, sep, value = item.strip().partition("=")
        if sep:
            values[key] = value
    token = values.get("_tb_token_")
    csrf = unquote(values.get("XSRF-TOKEN", ""))
    if not token or not csrf:
        raise ValueError("Cookie 缺少 _tb_token_ 或 XSRF-TOKEN，请复制该接口最新成功请求的完整 Cookie")
    return cookie, token, csrf


def fetch_coverage(config, cookie_path, timeout=30):
    payload = build_payload(config)
    cookie, token, csrf = credentials(cookie_path)
    query = urlencode({"bizCode": "dmp", "_tb_token_": token, "_csrf": csrf, "csrfId": csrf})
    request = Request(BASE_URL + "?" + query,
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "Cookie": cookie, "Origin": "https://dmp.taobao.com",
                               "Referer": "https://dmp.taobao.com/index_new.html",
                               "X-Requested-With": "XMLHttpRequest"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            final = urlsplit(response.geturl())
            if final.hostname != "dmp.taobao.com" or final.path != "/api_2/analysis/coverage":
                raise ValueError("请求被重定向，登录可能已失效，请更新 Cookie")
            raw = response.read().decode("utf-8-sig")
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("请求被重定向，登录可能已失效，请更新 Cookie") from None
        raise ValueError(f"HTTP {exc.code}：请检查登录状态、账号权限或访问限制") from None
    except (URLError, TimeoutError) as exc:
        raise ValueError("网络连接失败或超时，请检查网络后重试") from None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("接口未返回 JSON，可能登录失效或触发验证，请在浏览器中检查") from None
    return parse_result(config, result)


class LoginExpiredError(ValueError):
    pass


class Coverage401Error(LoginExpiredError):
    """An actual HTTP or business 401, rather than a missing local cookie."""
    pass


def wait_after_401(stop, notify):
    """Cool down for one minute, but let the user stop immediately."""
    notify("人数接口返回 401，暂停 1 分钟后重新获取页面会话并重试当前条件；可随时停止。")
    for remaining in (60, 30):
        if stop and stop.is_set():
            raise ValueError("已停止，已完成的结果已保留")
        if stop:
            if stop.wait(30):
                raise ValueError("已停止，已完成的结果已保留")
        else:
            time.sleep(30)
        if remaining > 30:
            notify(f"401 暂停中，剩余 {remaining - 30} 秒。")
    notify("401 暂停结束，正在恢复当前查询。")


def sync_browser_cookies(session, tab):
    session.cookies.clear()
    for item in tab.cookies(all_domains=True, all_info=True):
        domain = item.get("domain", "").lstrip(".")
        if domain and (domain == "dmp.taobao.com" or "dmp.taobao.com".endswith("." + domain)):
            session.cookies.set(item["name"], item["value"], domain=item["domain"],
                                path=item.get("path", "/"), secure=item.get("secure", False))


def new_browser_session(tab):
    import requests
    session = requests.Session()
    try:
        sync_browser_cookies(session, tab)
        prepared = session.prepare_request(requests.Request("POST", BASE_URL))
        values = dict(part.strip().split("=", 1) for part in
                      prepared.headers.get("Cookie", "").split(";") if "=" in part)
        if values.get("_tb_token_") and values.get("XSRF-TOKEN"):
            return session
    except Exception:
        session.close()
        raise
    session.close()
    return None


def refresh_browser_session(tab, stop=None, timeout=30):
    """Finish a real page reload before creating a fresh HTTP session."""
    if stop and stop.is_set():
        raise ValueError("已停止，已完成的结果已保留")
    current = urlsplit(str(tab.url or ""))
    if current.hostname == "dmp.taobao.com" and current.path == "/index_new.html":
        tab.refresh(ignore_cache=True)
    else:
        tab.get(PAGE_URL, timeout=timeout)
    # DrissionPage.refresh() returns at load_start; page text may still be from
    # the previous document at that point.
    return wait_browser_session(tab, stop, timeout)


def reopen_browser_session(browser, tab, stop=None, timeout=30):
    """Close the old DMP page and open a replacement without closing Chrome."""
    if stop and stop.is_set():
        raise ValueError("已停止，已完成的结果已保留")
    replacement = browser.new_tab()
    try:
        # Create the blank replacement before closing the old tab, otherwise
        # the last-tab close may terminate Chrome and its debugging connection.
        tab.close()
        replacement.set.window.max()
        replacement.get(PAGE_URL, timeout=timeout)
        replacement.set.activate()
        session = wait_browser_session(replacement, stop, timeout)
        # Keep the new page visible briefly after it is ready. A failed retry
        # otherwise closes it so quickly that the user cannot inspect it.
        if stop:
            if stop.wait(5):
                session.close()
                raise ValueError("已停止，已完成的结果已保留")
        else:
            time.sleep(5)
        return replacement, session
    except Exception:
        try:
            replacement.close()
        except Exception:
            pass
        raise


def wait_browser_session(tab, stop=None, timeout=30):
    """Wait for page load and asynchronous initialization, then read cookies."""
    if not tab.wait.doc_loaded(timeout=timeout):
        raise LoginExpiredError("达摩盘页面未完成加载，采集进度已保留；可稍后继续。")
    deadline = time.monotonic() + max(10, min(timeout, 30))
    settled = False
    while time.monotonic() < deadline:
        if stop and stop.is_set():
            raise ValueError("已停止，已完成的结果已保留")
        current = urlsplit(str(tab.url or ""))
        if current.hostname == "dmp.taobao.com" and current.path == "/index_new.html":
            body = tab.ele("tag:body", timeout=1)
            if body and "自定义圈人运算池" in str(body.text or ""):
                # Give the page's asynchronous session initialization time to
                # finish before copying cookies into a new requests.Session.
                if not settled:
                    if stop:
                        if stop.wait(2):
                            raise ValueError("已停止，已完成的结果已保留")
                    else:
                        time.sleep(2)
                    settled = True
                    continue
                session = new_browser_session(tab)
                if session:
                    return session
            else:
                settled = False
        if stop:
            stop.wait(1)
        else:
            time.sleep(1)
    raise LoginExpiredError("达摩盘页面仍未就绪。请检查浏览器是否出现验证页面，再继续未完成采集。")


def parse_result(config, result):
    if not isinstance(result, dict):
        raise ValueError("接口返回格式异常")
    info = result.get("info")
    if not isinstance(info, dict) or info.get("ok") is not True or info.get("code") != 0:
        code = info.get("code") if isinstance(info, dict) else None
        if str(code) == "401":
            raise Coverage401Error("人数接口返回 401，页面会话需要刷新。")
        raise ValueError(f"接口业务失败（code={code}），请在浏览器中检查登录状态和圈选条件")
    data = result.get("data")
    coverage = data.get("coverage") if isinstance(data, dict) else None
    if type(coverage) is not int or coverage < 0:
        raise ValueError("接口未返回有效的 data.coverage 人数")
    return {"coverage": coverage, "conditions": config, "data": data,
            "request_id": info.get("requestID"),
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def browser_options(user_data_dir, port=9351, channel="chrome", profile="Default"):
    from DrissionPage import ChromiumOptions
    options = ChromiumOptions(read_file=False)
    options.set_local_port(port)
    options.set_user_data_path(str(Path(user_data_dir).expanduser().resolve()))
    options.set_argument(f"--profile-directory={profile}")
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    options.set_argument("--start-maximized")
    browser_path = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if channel == "msedge":
        browser_path = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if browser_path.is_file():
        options.set_browser_path(str(browser_path))
    return options


def open_dmp_page(user_data_dir, port=9351, channel="chrome", profile="Default"):
    """Show the configured Chrome profile, reusing an existing DMP tab."""
    from DrissionPage import Chromium
    browser = Chromium(browser_options(user_data_dir, port, channel, profile))
    tabs = [tab for tab in browser.get_tabs(url="dmp.taobao.com")
            if urlsplit(str(tab.url or "")).hostname == "dmp.taobao.com"]
    reused = bool(tabs)
    tab = tabs[0] if reused else browser.new_tab()
    try:
        tab.set.window.max()
        if not reused:
            tab.get(PAGE_URL, timeout=30)
        tab.set.activate()
    except Exception:
        if not reused:
            try:
                tab.close()
            except Exception:
                pass
        raise
    return reused


def browser_coverage(config, user_data_dir, channel="chrome", profile="Default",
                     timeout=30, login_timeout=300, port=9351, *, query_configs=None,
                     on_result=None, stop=None, interval=1, log=None):
    """Use the same DrissionPage launch pattern as sycm_fw_ltjl."""
    jobs = list(query_configs) if query_configs is not None else [config]
    if not jobs:
        raise ValueError("没有需要查询的条件")
    for job in jobs:
        build_payload(job)
    if interval < 0:
        raise ValueError("请求间隔不能为负数")
    notify = log or (lambda message: print(message, file=sys.stderr))
    try:
        from DrissionPage import Chromium
        import requests
    except ImportError:
        raise ValueError("请先安装浏览器组件：python -m pip install -r requirements.txt") from None
    options = browser_options(user_data_dir, port, channel, profile)
    try:
        browser = Chromium(options)
        tab = browser.new_tab()
    except Exception:
        raise ValueError("浏览器启动失败：请检查浏览器安装、用户目录占用以及调试端口；不会强制关闭现有浏览器") from None
    completed = False
    try:
        tab.set.window.max()
        notify(f"已最大化打开浏览器（端口 {port}），正在进入达摩盘。")
        if not tab.get(PAGE_URL, timeout=timeout):
            notify("页面仍在加载，将继续等待。")
        deadline = time.monotonic() + login_timeout
        last_notice, last_navigation = 0.0, time.monotonic()
        ready, values = False, {}
        while time.monotonic() < deadline:
            if stop and stop.is_set():
                raise ValueError("已停止，已完成的结果已保留")
            current = urlsplit(str(tab.url or ""))
            now = time.monotonic()
            ready = False
            if current.hostname == "dmp.taobao.com" and current.path == "/index_new.html":
                body = tab.ele("tag:body", timeout=1)
                text = str(body.text or "") if body else ""
                ready = "自定义圈人运算池" in text
            if ready:
                session = new_browser_session(tab)
                if not session:
                    if stop:
                        stop.wait(1)
                    else:
                        time.sleep(1)
                    continue
                try:
                    notify("已取得浏览器登录状态，正在查询覆盖人数。")
                    results = []
                    for index, job in enumerate(jobs):
                        if stop and stop.is_set():
                            raise ValueError("已停止，已完成的结果已保留")
                        recovery_attempt = 0
                        while True:
                            if stop and stop.is_set():
                                raise ValueError("已停止，已完成的结果已保留")
                            try:
                                result = session_coverage(job, session, tab.user_agent, timeout)
                                break
                            except LoginExpiredError as exc:
                                if not isinstance(exc, Coverage401Error) and recovery_attempt >= 2:
                                    raise LoginExpiredError("页面会话恢复失败。采集进度已保留，失败的任务标签页已关闭；可点击页面顶部按钮打开达摩盘检查账号，随后继续未完成采集。") from None
                                session.close()
                                if isinstance(exc, Coverage401Error):
                                    wait_after_401(stop, notify)
                                recovery_attempt += 1
                                if recovery_attempt % 2 == 1:
                                    notify("正在刷新达摩盘并重新读取 Cookie。")
                                    try:
                                        session = refresh_browser_session(tab, stop, timeout)
                                        continue
                                    except LoginExpiredError:
                                        notify("刷新后页面未就绪，改为重新打开达摩盘页面。")
                                notify("人数接口仍返回 401，正在关闭旧页面并重新打开达摩盘，读取 Cookie 后重试当前查询。")
                                tab, session = reopen_browser_session(browser, tab, stop, timeout)
                                notify("新页面已加载并取得 Cookie，等待页面初始化后重试当前查询。")
                        results.append(result)
                        if on_result:
                            on_result(index, result)
                        if index + 1 < len(jobs):
                            if stop:
                                stop.wait(interval)
                            else:
                                time.sleep(interval)
                    completed = True
                    return results if query_configs is not None else results[0]
                finally:
                    session.close()
            # Login can finish on the Taobao homepage. Return to DMP afterward.
            if (current.hostname in {"www.taobao.com", "i.taobao.com", "dmp.taobao.com"}
                    and current.path in {"", "/", "/index.html"}
                    and now - last_navigation >= 5):
                tab.get(PAGE_URL, timeout=timeout)
                last_navigation = time.monotonic()
            if now - last_notice >= 15:
                location = f"{current.hostname or ''}{current.path or ''}"
                notify(f"等待登录或页面加载（{location}）；请在浏览器完成登录或验证。")
                last_notice = now
            time.sleep(1)
        tab.get_screenshot(path=str(ROOT), name="browser-status.png")
        raise ValueError("等待登录超时，任务标签页已关闭；完成登录后重新运行。截图：browser-status.png")
    except requests.RequestException:
        raise ValueError("人数接口请求失败或超时，任务标签页已关闭；请检查网络和页面状态") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("浏览器连接或页面操作失败，任务标签页已关闭；请检查是否关闭了任务页面") from None
    finally:
        if not completed:
            try:
                tab.close()
            except Exception:
                pass


def session_coverage(config, session, user_agent, timeout):
    import requests
    prepared = session.prepare_request(requests.Request("POST", BASE_URL))
    values = dict(part.strip().split("=", 1) for part in
                  prepared.headers.get("Cookie", "").split(";") if "=" in part)
    if not values.get("_tb_token_") or not values.get("XSRF-TOKEN"):
        raise LoginExpiredError("页面会话缺少查询令牌，需要刷新达摩盘页面")
    csrf = unquote(values["XSRF-TOKEN"])
    response = session.post(
        BASE_URL,
        params={"bizCode": "dmp", "_tb_token_": values["_tb_token_"], "_csrf": csrf, "csrfId": csrf},
        json=build_payload(config),
        headers={"Accept": "application/json", "Origin": "https://dmp.taobao.com",
                 "Referer": "https://dmp.taobao.com/index_new.html",
                 "X-Requested-With": "XMLHttpRequest", "User-Agent": user_agent},
        timeout=timeout, allow_redirects=False,
    )
    try:
        if response.status_code == 401:
            raise Coverage401Error("人数接口返回 401，页面会话需要刷新。")
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}：请检查浏览器登录、验证或账号权限")
        try:
            result = response.json()
        except ValueError:
            raise ValueError("接口未返回 JSON，请在浏览器检查登录或验证状态") from None
        return parse_result(config, result)
    finally:
        response.close()


def main():
    parser = argparse.ArgumentParser(description="达摩盘类目购买人群覆盖人数查询")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--cookie-file", type=Path, default=ROOT / "cookie.txt")
    parser.add_argument("--manual-cookie", action="store_true", help="使用 Cookie 文件或环境变量，不打开浏览器")
    parser.add_argument("--user-data-dir", type=Path, default=DEFAULT_USER_DATA, help="浏览器用户数据目录")
    parser.add_argument("--profile-directory", default="Default", help="用户数据目录中的配置名称，如 Default")
    parser.add_argument("--browser-port", type=int, default=9351, help="Chrome 调试端口，默认 9351")
    parser.add_argument("--browser", choices=["chrome", "msedge"], default="chrome")
    parser.add_argument("--login-timeout", type=float, default=300, help="等待人工登录的秒数")
    parser.add_argument("--start-date", help="覆盖配置中的开始日期")
    parser.add_argument("--end-date", help="覆盖配置中的结束日期")
    parser.add_argument("--output", type=Path, help="将查询结果保存为 JSON")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true", help="仅检查并打印请求体，不发送请求")
    args = parser.parse_args()
    try:
        if args.timeout <= 0 or args.login_timeout <= 0:
            raise ValueError("timeout 和 login-timeout 必须大于 0")
        if not 1 <= args.browser_port <= 65535:
            raise ValueError("browser-port 必须在 1–65535 之间")
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("配置文件必须为 JSON 对象")
        for key in ("start_date", "end_date"):
            if getattr(args, key):
                config[key] = getattr(args, key)
        if args.output:
            target = args.output.resolve()
            if target in {args.config.resolve(), args.cookie_file.resolve(), Path(__file__).resolve()} or target.is_relative_to(args.user_data_dir.resolve()):
                raise ValueError("输出路径不能覆盖配置、Cookie、脚本或浏览器用户目录")
        if args.dry_run:
            output = build_payload(config)
        elif args.manual_cookie:
            output = fetch_coverage(config, args.cookie_file, args.timeout)
        else:
            output = browser_coverage(config, args.user_data_dir, args.browser,
                                      args.profile_directory, args.timeout, args.login_timeout, args.browser_port)
        text = json.dumps(output, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
