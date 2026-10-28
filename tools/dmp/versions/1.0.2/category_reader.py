"""Read the full DMP category tree through the authenticated tag API."""

import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from coverage import DEFAULT_USER_DATA, PAGE_URL

CATEGORY_URL = "https://dmp.taobao.com/api_2/tag/224472"


def parse_category_response(body):
    try:
        if not isinstance(body, dict) or body.get("info", {}).get("ok") is not True or body["info"].get("code") != 0:
            raise ValueError("类目接口返回失败，请检查登录状态或账号权限")
        tag = body["data"]["tag"]
        if str(tag["id"]) != "224472" or not any(str(g["id"]) == "171470" for g in tag["optionGroups"]):
            raise ValueError("类目接口的标签或选项组不匹配，未更新缓存")
        options = tag["options"]
        if not isinstance(options, list):
            raise ValueError("类目接口数据结构已变化，未更新缓存")
        rows = [r for r in options if str(r["optionGroupId"]) == "171470"]
        if not rows:
            raise ValueError("类目接口列表为空，保留原缓存")
        nodes, values = {}, set()
        for row in rows:
            node_id, value, parent = str(row["id"]), str(row["optionValue"]), str(row["parentOptionId"])
            name = str(row["optionName"]).strip()
            if (not all(s.isascii() and s.isdigit() for s in (node_id, value, parent))
                    or node_id == "0" or not name or str(row["tagId"]) != "224472"):
                raise ValueError("类目接口包含无效节点，未更新缓存")
            if node_id in nodes or value in values:
                raise ValueError("类目接口包含重复节点，未更新缓存")
            nodes[node_id] = {"parent": parent, "category_id": value, "category_name": name}
            values.add(value)
        # parentOptionId references the internal option id, NOT category_id.
        checked = set()
        for identifier in nodes:
            current, path = identifier, set()
            while current != "0" and current not in checked:
                if current not in nodes or current in path:
                    raise ValueError("类目接口父子关系不完整或存在循环，未更新缓存")
                path.add(current)
                current = nodes[current]["parent"]
            checked.update(path)
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        parents, roots, indexed = [], {}, {}
        for identifier, node in nodes.items():
            if node["parent"] == "0":
                parent = {"category_id": node["category_id"], "category_name": node["category_name"],
                          "children": [], "children_loaded": True, "updated_at": updated_at}
                parents.append(parent)
                roots[identifier] = parent
                indexed[identifier] = parent
        if not parents:
            raise ValueError("类目接口缺少一级类目，保留原缓存")
        def attach(identifier):
            if identifier in indexed:
                return indexed[identifier]
            row = nodes[identifier]
            parent = attach(row["parent"])
            depth = 2 if row["parent"] in roots else 3 if "root_category_id" not in parent else 4
            if depth > 3:
                raise ValueError("类目接口出现超过三级的节点，未更新缓存")
            item = {"category_id": row["category_id"], "category_name": row["category_name"],
                    "parent_category_id": parent["category_id"], "children": [], "children_loaded": True}
            if depth == 3:
                item["root_category_id"] = parent["parent_category_id"]
            parent["children"].append(item)
            indexed[identifier] = item
            return item
        for identifier in nodes:
            attach(identifier)
        return {"parents": parents, "updated_at": updated_at, "full_tree": True}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("类目接口数据结构已变化，未更新缓存") from exc


class CategoryReader:
    def __init__(self, user_data_dir=DEFAULT_USER_DATA, port=9351, log=print, stop=None):
        self.user_data_dir, self.port = Path(user_data_dir), int(port)
        self.log, self.stop = log, stop
        self.tab = None

    def check_stop(self):
        if self.stop and self.stop.is_set():
            raise ValueError("已停止读取类目")

    def connect(self):
        from DrissionPage import Chromium, ChromiumOptions
        options = ChromiumOptions(read_file=False)
        options.set_local_port(self.port)
        options.set_user_data_path(str(self.user_data_dir.expanduser().resolve()))
        options.set_argument("--profile-directory=Default")
        options.set_argument("--start-maximized")
        options.set_argument("--no-first-run")
        options.set_argument("--no-default-browser-check")
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        if chrome.is_file():
            options.set_browser_path(str(chrome))
        browser = Chromium(options)
        # Reuse DMP only for login; category data comes from CATEGORY_URL.
        tabs = [tab for tab in browser.get_tabs(url="dmp.taobao.com")
                if urlsplit(str(tab.url)).hostname == "dmp.taobao.com"]
        self.tab = tabs[0] if tabs else browser.new_tab()
        self.tab.set.window.max()
        if not tabs:
            self.tab.get(PAGE_URL, timeout=30)

    def login_session(self, timeout=300):
        if self.tab is None:
            self.connect()
        deadline = time.monotonic() + timeout
        last_notice = 0
        while time.monotonic() < deadline:
            self.check_stop()
            if urlsplit(str(self.tab.url)).hostname == "dmp.taobao.com":
                session = requests.Session()
                try:
                    for cookie in self.tab.cookies(all_domains=True, all_info=True):
                        domain = cookie.get("domain", "").lstrip(".")
                        if domain and (domain == "dmp.taobao.com" or "dmp.taobao.com".endswith("." + domain)):
                            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"],
                                                path=cookie.get("path", "/"), secure=cookie.get("secure", False))
                    prepared = session.prepare_request(requests.Request("GET", CATEGORY_URL))
                    values = dict(p.strip().split("=", 1) for p in prepared.headers.get("Cookie", "").split(";") if "=" in p)
                    if values.get("_tb_token_") and values.get("XSRF-TOKEN"):
                        session.headers.update({"Accept": "application/json", "Referer": "https://dmp.taobao.com/index_new.html",
                                                "X-Requested-With": "XMLHttpRequest", "User-Agent": self.tab.user_agent})
                        return session, values
                except Exception:
                    session.close()
                    raise
                session.close()
            if time.monotonic() - last_notice >= 15:
                self.log("正在取得浏览器登录状态，请在达摩盘窗口完成登录或验证。")
                last_notice = time.monotonic()
            if self.stop:
                self.stop.wait(1)
            else:
                time.sleep(1)
        raise ValueError("等待登录超时，浏览器已保留，请登录后重新同步类目")

    def read(self, parent_id=None):
        self.check_stop()
        self.log("正在通过类目接口同步完整一级、二级、三级类目。")
        try:
            session, values = self.login_session()
            try:
                self.check_stop()
                csrf = unquote(values["XSRF-TOKEN"])
                response = session.get(CATEGORY_URL, params={"bizCode": "dmp", "appCode": "dmpBase",
                                       "_tb_token_": values["_tb_token_"], "_csrf": csrf, "csrfId": csrf},
                                       timeout=30, allow_redirects=False)
                try:
                    if response.status_code != 200:
                        raise ValueError(f"类目接口 HTTP {response.status_code}，请在浏览器检查登录、验证或账号权限")
                    try:
                        body = response.json()
                    except ValueError:
                        raise ValueError("类目接口未返回 JSON，请在浏览器检查登录或验证") from None
                    self.check_stop()
                    data = parse_category_response(body)
                finally:
                    response.close()
            finally:
                session.close()
        except requests.RequestException:
            raise ValueError("类目接口请求失败或超时，原缓存已保留，请检查网络后重试") from None
        if parent_id is not None:
            parent_id = str(parent_id)
            parent = next((p for p in data["parents"] if p["category_id"] == parent_id), None)
            if parent is None:
                raise ValueError("类目接口没有这个一级类目，请先同步一级列表")
            data.update(parent_id=parent_id, children=parent["children"])
        self.log(f"接口已读取 {len(data['parents'])} 个一级类目、"
                 f"{sum(len(p['children']) for p in data['parents'])} 个二级类目、"
                 f"{sum(len(c['children']) for p in data['parents'] for c in p['children'])} 个三级类目。")
        return data
