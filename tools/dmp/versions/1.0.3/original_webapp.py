"""Local web UI for category discovery and recoverable coverage collection."""

import argparse
import copy
import hmac
import json
import re
import secrets
import socket
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from batch import check_previous, export_excel, make_plan, periods_between, result_key, save_progress, load_progress, replace_file, ProgressSaveError
from category_catalog import CategoryCatalog
from category_reader import CategoryReader
from coverage import DEFAULT_USER_DATA, ROOT, browser_coverage, open_dmp_page

OUTPUTS = ROOT / "outputs"
RESULT_NAME = "覆盖人数.xlsx"
PROGRESS_NAME = "覆盖人数.progress.json"


def normalize_filename(value):
    """Accept one Windows-safe Excel basename, with an optional .xlsx suffix."""
    if value is None or value == "":
        return RESULT_NAME
    if not isinstance(value, str):
        raise ValueError("导出文件名无效")
    name = value.strip()
    if not name:
        return RESULT_NAME
    if name.lower().endswith(".xlsx"):
        name = name[:-5]
    if (not name or len(name) > 100 or name[-1] in ". " or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or
            re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
        raise ValueError("导出文件名只能填写名称，不能包含路径或 Windows 禁用字符")
    return name + ".xlsx"


def normalize_price_ranges(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("请至少填写一个金额区间")
    ranges = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("金额区间格式无效")
        ranges.append({"min_amount": str(row.get("min_amount", "")).strip(),
                       "max_amount": str(row.get("max_amount", "")).strip(),
                       "highlight": row.get("highlight", False) is True})
    # Use the same amount and duplicate checks as a real collection plan.
    make_plan({"query_mode": "second", "categories": [{"category_id": "1", "category_name": "校验", "level": 2}],
               "periods": [{"start_date": "2026-01-01", "end_date": "2026-01-01"}],
               "price_ranges": ranges})
    return ranges


def normalize_settings(payload, catalog, price_presets=None, category_templates=None):
    mode = payload.get("query_mode", "second")
    grouped = payload.get("selected_ids_by_level")
    if grouped is not None or mode == "mixed":
        if grouped is None:
            saved = payload.get("categories", [])
            grouped = {"second": [c["category_id"] for c in saved if c.get("level") == 2],
                       "third": [c["category_id"] for c in saved if c.get("level") == 3]}
        if not isinstance(grouped, dict) or any(not isinstance(grouped.get(level, []), list)
                                                 for level in ("first", "second", "third")):
            raise ValueError("类目选择格式无效")
        if mode == "first":
            categories = catalog.resolve(grouped.get("first", []), "first")
        elif mode in {"second", "third", "mixed"}:
            second = catalog.resolve(grouped.get("second", []), "second")
            third = catalog.resolve(grouped.get("third", []), "third")
            categories = second + third
            mode = "mixed" if second and third else "second" if second else "third" if third else "second"
        else:
            raise ValueError("未知的类目查询方式")
    else:
        ids = payload.get("selected_ids", [c["category_id"] for c in payload.get("categories", [])])
        categories = catalog.resolve(ids, mode)
    interval, port = float(payload.get("interval", 1)), int(payload.get("port", 9351))
    if not 0.2 <= interval <= 3600 or not 1 <= port <= 65535:
        raise ValueError("请求间隔须为 0.2–3600 秒，浏览器端口须为 1–65535")
    user_dir = str(payload.get("user_data_dir", DEFAULT_USER_DATA)).strip()
    if not user_dir:
        raise ValueError("浏览器用户目录不能为空")
    start, end = str(payload["start_date"]).strip(), str(payload["end_date"]).strip()
    monthly = payload.get("monthly", True)
    if not isinstance(monthly, bool):
        raise ValueError("按月拆分参数无效")
    price_ranges = normalize_price_ranges(payload["price_ranges"])
    price_presets = price_presets or {}
    category_templates = category_templates or {}
    names = {}
    category_price_ranges = {}
    for category in categories:
        identifier = str(category["category_id"])
        template = category_templates.get(identifier)
        if isinstance(template, dict):
            names[identifier] = "自定义"
            category_price_ranges[identifier] = copy.deepcopy(normalize_price_ranges(template.get("custom_price_ranges")))
        elif template:
            if template not in price_presets:
                raise ValueError(f"{category['category_name']} 的金额模板“{template}”不存在，请重新分配")
            names[identifier] = template
            category_price_ranges[identifier] = copy.deepcopy(price_presets[template])
    settings = {"query_mode": mode, "categories": categories, "start_date": start, "end_date": end,
                "monthly": monthly, "periods": periods_between(start, end, monthly),
                "price_ranges": price_ranges, "category_template_names": names,
                "category_price_ranges": category_price_ranges,
                "user_data_dir": user_dir, "port": port, "interval": interval,
                "filename": normalize_filename(payload.get("filename"))}
    make_plan(settings)
    return settings


class LocalApplication:
    def __init__(self, outputs=None, catalog=None, settings_path=None, presets_path=None,
                 price_presets_path=None, category_templates_path=None):
        self.catalog = catalog or CategoryCatalog()
        self.catalog_revision = secrets.token_hex(8)
        self.outputs = Path(outputs) if outputs else OUTPUTS
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.settings_path = Path(settings_path) if settings_path else ROOT / "ui-settings.json"
        self.presets_path = Path(presets_path) if presets_path else ROOT / "data/category_presets.json"
        self.price_presets_path = Path(price_presets_path) if price_presets_path else ROOT / "data/price_presets.json"
        self.category_templates_path = (Path(category_templates_path) if category_templates_path else
                                        self.price_presets_path.parent / "category_template_assignments.json")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.clear_worker = None
        self.clearing = False
        self.browser_opening = False
        self.active_checkpoint = PROGRESS_NAME
        self.closing = False
        self.notice = self.catalog.load_error
        self.task = {"running": False, "kind": None, "status": "就绪", "completed": 0, "total": 0, "logs": [], "output": None, "error": None}
        if (self.outputs / RESULT_NAME).is_file():
            self.task.update(status="已有结果", output=RESULT_NAME)
        self.settings = {"query_mode": "second", "categories": self.catalog.resolve(["350213"], "second"),
                         "start_date": "2026-08-01", "end_date": "2026-08-31", "monthly": True,
                         "user_data_dir": str(DEFAULT_USER_DATA), "port": 9351, "interval": 1,
                         "price_ranges": [{"min_amount": str(a), "max_amount": str(b), "highlight": a in (90, 300)}
                                          for a, b in [(0,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),
                                                       (80,90),(90,100),(100,150),(150,200),(200,250),(250,300),(300,350),(350,400)]]}
        try:
            self.presets = json.loads(self.presets_path.read_text(encoding="utf-8")) if self.presets_path.exists() else {}
        except (ValueError, OSError):
            self.presets = {}
        self.price_presets = {}
        if self.price_presets_path.exists():
            try:
                saved = json.loads(self.price_presets_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("金额配置文件格式无效")
                for name, ranges in saved.items():
                    try:
                        if isinstance(name, str) and 1 <= len(name.strip()) <= 80:
                            self.price_presets[name] = normalize_price_ranges(ranges)
                    except (ValueError, KeyError, TypeError):
                        continue
            except (ValueError, OSError):
                self.notice = "已保存的金额区间配置无法读取，请重新保存。"
        self.category_template_assignments = {}
        if self.category_templates_path.exists():
            try:
                saved = json.loads(self.category_templates_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("类目金额模板分配文件格式无效")
                for identifier, assignment in saved.items():
                    if not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit():
                        continue
                    if isinstance(assignment, str) and assignment in self.price_presets:
                        self.category_template_assignments[identifier] = assignment
                    elif isinstance(assignment, dict) and set(assignment) == {"custom_price_ranges"}:
                        try:
                            self.category_template_assignments[identifier] = {
                                "custom_price_ranges": normalize_price_ranges(assignment["custom_price_ranges"])}
                        except (ValueError, KeyError, TypeError):
                            continue
                if len(self.category_template_assignments) != len(saved):
                    self.notice = "部分类目金额模板已失效，请重新分配。"
            except (ValueError, OSError):
                self.notice = "类目金额模板分配无法读取，请重新分配。"
        if self.settings_path.exists():
            try:
                self.settings = normalize_settings(json.loads(self.settings_path.read_text(encoding="utf-8")), self.catalog,
                                                   self.price_presets, self.category_template_assignments)
            except (ValueError, OSError, KeyError, TypeError):
                self.notice = "上次设置不适用于当前类目库，请重新选择类目。"
        saved_output = self.outputs / self.settings.get("filename", RESULT_NAME)
        if saved_output.is_file():
            self.task.update(status="已有结果", output=saved_output.name)

    def snapshot(self, catalog_revision=None):
        with self.lock:
            checkpoint = self.outputs / self.active_checkpoint
            resume_file = self.active_checkpoint if (checkpoint.is_file() or checkpoint.with_suffix(checkpoint.suffix + '.tmp').is_file()) else None
            data = {"settings": self.settings, "catalog_revision": self.catalog_revision,
                                  "catalog_complete": self.catalog.complete, "catalog_updated_at": self.catalog.updated_at,
                                  "presets": self.presets, "price_presets": self.price_presets,
                                  "category_template_assignments": self.category_template_assignments,
                                  "task": {**self.task, "status": "正在停止任务并清空…" if self.clearing else self.task["status"]},
                                  "notice": self.notice, "resume_file": resume_file, "clearing": self.clearing,
                                  "output_dir": str(self.outputs.resolve())}
            if catalog_revision != self.catalog_revision:
                data["parents"] = list(self.catalog.parents.values())
            return copy.deepcopy(data)

    def log(self, message):
        with self.lock:
            self.task["logs"] = (self.task["logs"] + [str(message)])[-200:]

    def begin(self, kind, action):
        with self.lock:
            if self.closing:
                raise ValueError("页面已关闭，服务正在退出，请重新启动程序")
            if self.clearing:
                raise ValueError("正在清空进度与结果，请稍候")
            if self.browser_opening:
                raise ValueError("正在打开达摩盘，请稍候")
            if self.task["running"]:
                raise ValueError("当前有任务正在执行，请先等待或停止任务")
            self.stop_event.clear()
            self.task = {"running": True, "kind": kind, "status": "执行中", "completed": 0, "total": 0,
                         "logs": [], "output": None, "error": None}
            def run():
                try:
                    action()
                except Exception as exc:
                    # Browser library errors can embed URLs and credentials.
                    text = str(exc) if isinstance(exc, (ValueError, OSError)) else "任务执行失败，请检查浏览器、登录状态或网络后重试。"
                    self.log(text)
                    with self.lock:
                        self.task.update(status="已停止" if self.stop_event.is_set() else "失败", error=text)
                finally:
                    with self.lock:
                        self.task["running"] = False
            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()

    def open_browser(self, payload):
        user_dir = str(payload.get("user_data_dir", DEFAULT_USER_DATA)).strip()
        port = int(payload.get("port", 9351))
        if not user_dir or not 1 <= port <= 65535:
            raise ValueError("浏览器用户目录或端口无效")
        with self.lock:
            if self.closing:
                raise ValueError("页面已关闭，服务正在退出，请重新启动程序")
            if self.task["running"] or self.clearing:
                raise ValueError("当前任务执行期间不能切换账号，请先停止或等待完成")
            if self.browser_opening:
                raise ValueError("正在打开达摩盘，请稍候")
            self.browser_opening = True
        try:
            return {"reused": open_dmp_page(Path(user_dir), port)}
        except ImportError:
            raise ValueError("请先安装浏览器组件：python -m pip install -r requirements.txt") from None
        except Exception:
            raise ValueError("打开达摩盘失败，请检查浏览器安装、用户目录和调试端口") from None
        finally:
            with self.lock:
                self.browser_opening = False

    def refresh(self, payload):
        parent_id = payload.get("parent_id")
        user_dir, port = str(payload.get("user_data_dir", DEFAULT_USER_DATA)).strip(), int(payload.get("port", 9351))
        if not user_dir or not 1 <= port <= 65535:
            raise ValueError("浏览器用户目录或端口无效")
        if parent_id is not None and str(parent_id) not in self.catalog.parents:
            raise ValueError("请先刷新一级类目列表")
        def work():
            reader = CategoryReader(user_dir, port, self.log, self.stop_event)
            data = reader.read(parent_id)
            if self.stop_event.is_set():
                raise ValueError("已停止读取类目，原缓存已保留")
            with self.lock:
                updated = copy.deepcopy(self.catalog)
                updated.replace_parents(data["parents"], data["updated_at"], complete=True)
                if data.get("parent_id") and not data.get("full_tree"):
                    updated.set_children(data["parent_id"], data["children"], data["updated_at"])
                updated.save()
                updated.load_error = None
                self.catalog = updated
                self.catalog_revision = secrets.token_hex(8)
                self.task["status"] = "类目已更新"
            self.log(f"已读取 {len(data['parents'])} 个一级类目" + (f"，当前二级类目 {len(data['children'])} 个。" if data.get("children") else "。"))
        self.begin("categories", work)

    def save_settings(self, payload):
        with self.lock:
            if self.clearing:
                raise ValueError("正在清空进度与结果，请稍候")
            if self.task["running"]:
                raise ValueError("任务执行期间不能改写设置，请等待任务结束")
            settings = normalize_settings(payload, self.catalog, self.price_presets, self.category_template_assignments)
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.settings_path)
            self.settings = settings
            return settings

    def save_default_price_ranges(self, payload):
        ranges = normalize_price_ranges(payload.get("price_ranges"))
        with self.lock:
            if self.closing or self.clearing or self.task["running"]:
                raise ValueError("任务运行期间不能修改默认金额区间")
            updated = copy.deepcopy(self.settings)
            updated["price_ranges"] = ranges
            make_plan(updated)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_file(temp, self.settings_path)
            self.settings = updated
            return {"price_ranges": ranges}

    def preview(self, payload):
        with self.lock:
            settings = normalize_settings(payload, self.catalog, self.price_presets, self.category_template_assignments)
        return {"settings": settings, "count": len(make_plan(settings))}

    def output_path(self, name, suffix):
        relative = Path(str(name))
        target = (self.outputs / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(self.outputs.resolve()) or target.suffix.lower() != suffix:
            raise ValueError("文件路径无效")
        return target

    def reveal_output(self, name):
        path = self.output_path(name, ".xlsx")
        if not path.is_file():
            raise ValueError("Excel 文件不存在，请先完成采集")
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return path

    def history_files(self):
        """List old output files for explicit review; never include the current slot."""
        with self.lock:
            root = self.outputs.resolve()
            protected = {root / PROGRESS_NAME, root / (PROGRESS_NAME + '.tmp')}
            if self.task.get('output'):
                protected.add(self.output_path(self.task['output'], '.xlsx'))
            protected.add(self.output_path(self.settings.get('filename', RESULT_NAME), '.xlsx'))
            files = []
            for path in root.rglob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                target = path.resolve()
                if not target.is_relative_to(root) or target in protected:
                    continue
                stat = path.stat()
                # JavaScript numbers cannot preserve nanosecond timestamps. Keep the
                # reviewed value exact when the browser sends it back for deletion.
                files.append({'path': path.relative_to(root).as_posix(), 'size': stat.st_size,
                              'mtime_ns': str(stat.st_mtime_ns)})
            return sorted(files, key=lambda item: item['path'].casefold())

    def cleanup_history(self, payload):
        """Delete only the unchanged files the user reviewed in the dialog."""
        if payload.get('confirm') is not True:
            raise ValueError('请先确认要清理的历史文件')
        selected = payload.get('files')
        if not isinstance(selected, list) or not selected or len(selected) > 5000:
            raise ValueError('请先选择要清理的历史文件')
        with self.lock:
            if self.closing or self.clearing or self.task['running']:
                raise ValueError('任务运行期间不能清理历史文件')
            listed = {item['path']: item for item in self.history_files()}
            names = []
            for item in selected:
                if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                    raise ValueError('历史文件清单无效')
                name = item['path']
                if name in names or name not in listed or item != listed[name]:
                    raise ValueError('历史文件已变化，请重新查看清单')
                names.append(name)
            root = self.outputs.resolve()
            for name in names:
                target = root / name
                if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
                    raise ValueError('历史文件已变化，请重新查看清单')
                stat = target.stat()
                if (stat.st_size, str(stat.st_mtime_ns)) != (listed[name]['size'], listed[name]['mtime_ns']):
                    raise ValueError('历史文件已变化，请重新查看清单')
            for name in names:
                target = root / name
                try:
                    target.unlink()
                except PermissionError:
                    raise ValueError('文件被占用，请关闭相关 Excel 或预览后再清理') from None
                parent = target.parent
                while parent != root and parent.is_relative_to(root):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            return {'deleted': len(names)}

    def start(self, payload):
        # Reserve the task while persisting the reviewed settings, avoiding a race
        # between two tabs pressing Start at the same time.
        with self.lock:
            if self.clearing:
                raise ValueError("正在清空进度与结果，请稍候")
            if self.task["running"]:
                raise ValueError("当前有任务正在执行，请先等待或停止任务")
            settings = normalize_settings(payload, self.catalog, self.price_presets, self.category_template_assignments)
            checkpoint = self.outputs / PROGRESS_NAME
            if checkpoint.is_file() or checkpoint.with_suffix('.json.tmp').is_file():
                raise ValueError("存在未完成采集，请先继续采集，或清空进度与结果后开始新任务")
            output = self.output_path(settings["filename"], ".xlsx")
            settings = self.save_settings(payload)
            self.begin("coverage", lambda: self.collect(settings, output, []))

    def resume(self, name):
        with self.lock:
            if self.clearing:
                raise ValueError("正在清空进度与结果，请稍候")
            if self.task["running"]:
                raise ValueError("请先等待当前任务结束")
            if name != PROGRESS_NAME:
                raise ValueError("只能继续当前采集进度")
            checkpoint = self.output_path(PROGRESS_NAME, ".json")
            saved = load_progress(checkpoint)
            settings, results = saved["settings"], saved["results"]
            check_previous(settings, results)
            output = self.output_path(normalize_filename(settings.get("filename")), ".xlsx")
            self.begin("coverage", lambda: self.collect(settings, output, results))

    def clear_results(self):
        with self.lock:
            if self.closing:
                raise ValueError("服务正在退出，请重新启动程序")
            if self.clearing:
                raise ValueError("正在清空进度与结果，请稍候")
            checkpoint = self.output_path(self.active_checkpoint, '.json')
            if not checkpoint.name.endswith('.progress.json'):
                raise ValueError("当前进度文件路径无效")
            if checkpoint.is_file() or checkpoint.with_suffix(checkpoint.suffix + '.tmp').is_file():
                try:
                    saved = load_progress(checkpoint)
                    output_name = normalize_filename(saved['settings'].get('filename'))
                except (ValueError, KeyError, TypeError, OSError):
                    output_name = self.task.get('output') or self.settings.get('filename', RESULT_NAME)
            else:
                output_name = self.task.get('output') or self.settings.get('filename', RESULT_NAME)
            output = self.output_path(output_name, '.xlsx')
            temporary = output.with_name('.' + output.stem + '.exporting.xlsx')
            targets = [output, checkpoint, checkpoint.with_suffix(checkpoint.suffix + '.tmp'), temporary,
                       Path(str(temporary) + '.inspect.ndjson'), Path(str(output) + '.inspect.ndjson')]
            root = self.outputs.resolve()
            if any(not p.resolve().is_relative_to(root) or p.is_dir() for p in targets):
                raise ValueError("清空路径无效")
            self.clearing = True
            self.stop_event.set()
            previous_worker = self.worker

            def clear():
                try:
                    if previous_worker:
                        previous_worker.join(timeout=220)
                        if previous_worker.is_alive():
                            raise ValueError("当前任务仍在结束，请稍后重新清空")
                    # Wait until collection/export has fully stopped before deleting
                    # files, so the old worker cannot recreate the cleared results.
                    for path in targets:
                        for delay in (0.05, 0.1, 0.2, 0.4, 0.8, None):
                            try:
                                path.unlink(missing_ok=True)
                                break
                            except PermissionError:
                                if delay is None:
                                    raise ValueError("结果文件被占用，请关闭已打开的 Excel 或文件预览后重新清空") from None
                                time.sleep(delay)
                    with self.lock:
                        self.worker = None
                        self.task = {"running": False, "kind": None, "status": "已清空", "completed": 0,
                                     "total": 0, "logs": [], "output": None, "error": None}
                except (ValueError, OSError) as exc:
                    with self.lock:
                        self.task.update(status="清空失败", error=str(exc))
                finally:
                    with self.lock:
                        self.clearing = False

            self.clear_worker = threading.Thread(target=clear, daemon=True)
            self.clear_worker.start()

    def collect(self, settings, output, previous):
        results = list(previous)
        checkpoint = self.outputs / PROGRESS_NAME
        jobs = make_plan(settings)
        completed = {result_key(r["conditions"]) for r in results}
        pending = [job for job in jobs if result_key(job) not in completed]
        status, error = "完成", None
        with self.lock:
            self.task.update(total=len(jobs), completed=len(results))
        def on_result(index, result):
            results.append(result)
            save_progress(checkpoint, settings, results)
            c = result["conditions"]
            self.log(f"{c['category_name']} {c['start_date']} {c['min_amount']}~{c['max_amount']}：{result['coverage']} 人")
            with self.lock:
                self.task["completed"] = len(results)
        try:
            save_progress(checkpoint, settings, results)
            if pending:
                browser_coverage(pending[0], Path(settings["user_data_dir"]), port=settings["port"],
                                 query_configs=pending, on_result=on_result, stop=self.stop_event,
                                 interval=settings["interval"], log=self.log)
        except Exception as exc:
            status = "已停止" if self.stop_event.is_set() else "未完成"
            error = str(exc) if isinstance(exc, (ValueError, OSError)) else "查询失败，请检查登录状态或网络。"
            self.log(error)
        source = checkpoint
        try:
            save_progress(checkpoint, settings, results, status, error)
        except ProgressSaveError as exc:
            source = exc.recovery_path
            status, error = "未完成", str(exc)
            self.log(error)
        self.log("正在导出 Excel；未完成的条件显示“未查询”。")
        temporary = output.with_name('.' + output.stem + '.exporting.xlsx')
        try:
            export_excel(source, temporary)
            try:
                replace_file(temporary, output)
            except PermissionError as exc:
                raise ValueError(f"Excel 结果文件“{output.name}”无法覆盖，可能正在 Excel 中打开或被其他程序占用。请关闭该文件后点击“继续未完成采集”；已完成的 {len(results)} 条查询不会重复执行。") from exc
        except Exception as exc:
            if isinstance(exc, (ValueError, OSError)):
                raise
            raise ValueError(f"Excel 导出失败（{type(exc).__name__}）：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + '.inspect.ndjson').unlink(missing_ok=True)
        if status == "完成":
            checkpoint.unlink(missing_ok=True)
            checkpoint.with_suffix('.json.tmp').unlink(missing_ok=True)
            Path(str(output) + '.inspect.ndjson').unlink(missing_ok=True)
        with self.lock:
            self.task.update(status=status, error=error, completed=len(results), output=str(output.relative_to(self.outputs)).replace('\\', '/'))
        self.log("Excel 已保存，可点击下载。")

    def preset(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("请填写 1–80 字的组合名称")
        mode = payload["mode"]
        with self.lock:
            if mode == "mixed":
                groups = payload.get("ids_by_level", {})
                if not isinstance(groups, dict):
                    raise ValueError("类目组合格式无效")
                second = self.catalog.resolve(groups.get("second", []), "second")
                third = self.catalog.resolve(groups.get("third", []), "third")
                if not second or not third:
                    raise ValueError("混合组合须同时选择二级和三级类目")
                preset = {"mode": "mixed", "ids_by_level": {
                    "second": [c["category_id"] for c in second],
                    "third": [c["category_id"] for c in third]}}
            else:
                nodes = self.catalog.resolve(payload["ids"], mode)
                if not nodes:
                    raise ValueError("请先选择类目")
                preset = {"mode": mode, "ids": [c["category_id"] for c in nodes]}
            updated = dict(self.presets)
            updated[name] = preset
            self.presets_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.presets_path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.presets_path)
            self.presets = updated

    def price_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("请填写 1–80 字的金额配置名称")
        action = payload.get("action")
        with self.lock:
            if self.closing or self.clearing:
                raise ValueError("服务正在退出或清空，请稍后重试")
            updated = dict(self.price_presets)
            if action == "save":
                updated[name] = normalize_price_ranges(payload.get("price_ranges"))
            elif action == "delete":
                if name not in updated:
                    raise ValueError("所选金额配置不存在")
                users = [identifier for identifier, template in self.category_template_assignments.items() if template == name]
                if users:
                    raise ValueError(f"金额配置“{name}”已分配给 {len(users)} 个类目，请先将这些类目改为默认或其他模板")
                del updated[name]
            else:
                raise ValueError("未知的金额配置操作")
            self.price_presets_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.price_presets_path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_file(temp, self.price_presets_path)
            self.price_presets = updated

    def category_template(self, payload):
        identifiers = payload.get("category_ids")
        name = payload.get("template", "")
        custom = "custom_price_ranges" in payload
        if (not isinstance(identifiers, list) or not identifiers or len(identifiers) > 1000
                or any(not isinstance(item, str) or not item.isascii() or not item.isdigit() for item in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError("请选择有效的类目")
        if custom and (len(identifiers) != 1 or "template" in payload):
            raise ValueError("单独设置类目金额区间时，请只选择一个类目")
        if not isinstance(name, str):
            raise ValueError("金额模板名称无效")
        ranges = normalize_price_ranges(payload["custom_price_ranges"]) if custom else None
        with self.lock:
            if self.closing or self.clearing or self.task["running"]:
                raise ValueError("任务运行期间不能修改类目金额模板")
            if name and name not in self.price_presets:
                raise ValueError("所选金额模板不存在，请先保存金额配置")
            known = {parent["category_id"] for parent in self.catalog.parents.values()}
            for parent in self.catalog.parents.values():
                for child in parent["children"]:
                    known.add(child["category_id"])
                    known.update(grand["category_id"] for grand in child["children"])
            if any(identifier not in known for identifier in identifiers):
                raise ValueError("所选类目已不在当前类目库中，请重新同步")
            updated = dict(self.category_template_assignments)
            for identifier in identifiers:
                if custom:
                    updated[identifier] = {"custom_price_ranges": ranges}
                elif name:
                    updated[identifier] = name
                else:
                    updated.pop(identifier, None)
            self.category_templates_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.category_templates_path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_file(temp, self.category_templates_path)
            self.category_template_assignments = updated
            return {"updated": len(identifiers)}


class PageSessions:
    """Track tabs separately; allow reloads and tolerate background timer throttling."""

    def __init__(self, clock=time.monotonic, close_grace=3, lease=150, startup_grace=60):
        self.clock, self.close_grace, self.lease = clock, close_grace, lease
        self.startup_deadline = clock() + startup_grace
        self.pages = {}
        self.empty_since = None
        self.seen_page = False
        self.closing = False
        self.lock = threading.Lock()

    def update(self, page_id, closed=False):
        if not isinstance(page_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', page_id):
            raise ValueError("页面标识无效")
        with self.lock:
            if self.closing:
                raise ValueError("服务正在退出，请重新启动程序")
            if closed:
                self.seen_page = True
                self.pages.pop(page_id, None)
                if self.seen_page and not self.pages and self.empty_since is None:
                    self.empty_since = self.clock()
            else:
                self.pages[page_id] = self.clock()
                self.seen_page = True
                self.empty_since = None

    def should_stop(self):
        with self.lock:
            if self.closing:
                return False
            now = self.clock()
            self.pages = {key: stamp for key, stamp in self.pages.items() if now - stamp < self.lease}
            if self.pages:
                return False
            if self.seen_page:
                if self.empty_since is None:
                    self.empty_since = now
                expired = now - self.empty_since >= self.close_grace
            else:
                expired = now >= self.startup_deadline
            if expired:
                self.closing = True
            return expired


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, app):
        self.app, self.token = app, secrets.token_urlsafe(32)
        self.pages = PageSessions()
        super().__init__(address, Handler)

    def service_actions(self):
        if self.pages.should_stop():
            with self.app.lock:
                self.app.closing = True
                self.app.stop_event.set()
            self.app.log("采集页面已全部关闭，正在停止任务并退出服务。")
            # shutdown must run outside the serve_forever thread.
            threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def watch_page(self, page_id):
        self.server.pages.update(page_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.connection.settimeout(2)
        try:
            while not self.server.app.closing:
                self.wfile.write(b": page connected\n\n")
                self.wfile.flush()
                time.sleep(1)
        except OSError:
            pass
        finally:
            try:
                self.server.pages.update(page_id, closed=True)
            except ValueError:
                pass

    def log_message(self, *args):
        pass

    def send(self, status, data, mime="application/json; charset=utf-8", filename=None):
        content = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(content)

    def valid_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "仅允许本机访问"})
        parts = urlsplit(self.path)
        try:
            if parts.path == "/":
                html = (ROOT / "web/index.html").read_text(encoding="utf-8").replace("__LOCAL_TOKEN__", self.server.token)
                return self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if parts.path == "/api/state":
                revision = parse_qs(parts.query).get("catalog_revision", [None])[0]
                return self.send(200, self.server.app.snapshot(revision))
            if parts.path == "/api/history":
                return self.send(200, {"files": self.server.app.history_files()})
            if parts.path == "/api/download":
                name = parse_qs(parts.query).get("file", [""])[0]
                suffix = ".xlsx" if name.lower().endswith(".xlsx") else ".json"
                path = self.server.app.output_path(name, suffix)
                mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if suffix == ".xlsx" else "application/json"
                return self.send(200, path.read_bytes(), mime, path.name)
            return self.send(404, {"error": "页面不存在"})
        except (ValueError, OSError) as exc:
            return self.send(400, {"error": str(exc)})

    def do_POST(self):
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if (not self.valid_host() or self.headers.get("Origin", next(iter(allowed))) not in allowed):
            return self.send(403, {"error": "请求来源无效，请刷新本地页面"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
            app = self.server.app
            route = urlsplit(self.path).path
            token = self.headers.get("X-Local-Token", "")
            # sendBeacon cannot set custom headers; accept its body token only
            # on the tab-close endpoint, with the same Host/Origin restrictions.
            if route == "/api/page/close":
                token = str(payload.get("_token", ""))
            if not hmac.compare_digest(token, self.server.token):
                return self.send(403, {"error": "请求来源无效，请刷新本地页面"})
            if route == "/api/page/watch":
                return self.watch_page(payload.get("page_id"))
            if route in {"/api/page/ping", "/api/page/close"}:
                self.server.pages.update(payload.get("page_id"), closed=route.endswith("/close"))
                return self.send(200, {"ok": True})
            if route == "/api/reveal-output":
                path = app.reveal_output(payload.get("file", ""))
                return self.send(200, {"path": str(path)})
            if route == "/api/browser/open":
                return self.send(200, app.open_browser(payload))
            if route == "/api/history/cleanup":
                return self.send(200, app.cleanup_history(payload))
            if route == "/api/preview":
                return self.send(200, app.preview(payload))
            if route == "/api/default-price-ranges":
                return self.send(200, app.save_default_price_ranges(payload))
            if route == "/api/category-template":
                return self.send(200, app.category_template(payload))
            if route == "/api/settings":
                app.save_settings(payload)
            elif route == "/api/categories/refresh":
                app.refresh(payload)
            elif route == "/api/start":
                app.start(payload)
            elif route == "/api/resume":
                app.resume(payload["file"])
            elif route == "/api/stop":
                app.stop_event.set()
                app.log("已请求停止，正在保留已完成的结果。")
            elif route == "/api/clear":
                app.clear_results()
            elif route == "/api/preset":
                app.preset(payload)
            elif route == "/api/price-preset":
                app.price_preset(payload)
            else:
                return self.send(404, {"error": "操作不存在"})
            self.send(200, {"ok": True})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="达摩盘覆盖人数本地网页")
    parser.add_argument("--port", type=int, default=18765, help="本地网页端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开网页")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port 必须在 1–65535 之间")
    try:
        server = LocalServer(("127.0.0.1", args.port), LocalApplication())
    except OSError:
        print(f"端口 {args.port} 已被占用。若程序已启动，请打开 http://127.0.0.1:{args.port}；或使用 --port 更换端口。")
        return 1
    address = f"http://127.0.0.1:{args.port}"
    print(f"达摩盘采集网页已启动：{address}\n关闭最后一个采集页面后自动停止任务并释放端口。", flush=True)
    if not args.no_open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.app.stop_event.set()
    finally:
        server.app.closing = True
        server.app.stop_event.set()
        server.server_close()
        # Release the listening port first, then let the in-flight request and
        # final export finish. Each result already has a durable checkpoint.
        if server.app.worker:
            server.app.worker.join(timeout=220)
        if server.app.clear_worker:
            server.app.clear_worker.join(timeout=220)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
