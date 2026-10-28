# 边界BI词根分析采集

双击 **启动采集.cmd**，自动打开本地网页 `http://127.0.0.1:18766`，选择类目和时间，再点击“开始采集并保存 Excel”。需要 Windows、Python 3.10+ 和本机 Google Chrome。启动器会创建项目内的 `.venv` 并安装依赖。

## 使用步骤

1. 首次运行点击“打开边界BI”，在弹出的 Chrome 中正常登录。以后复用该资料目录的登录状态。
2. 点击“同步一级类目”，读取当前账号可访问的类目。选择一级类目；需要更细时，依次点击“读取二级类目”和“读取三级类目”。下级不选则查询上级。
3. 选择近7天、近14天、近30天、指定周或指定月。点击“查看可用日期”可查看该类目允许的时间范围。
4. 点击“开始采集并保存 Excel”。每次都会打开产品需求分析页面，重新读取 Cookie，并使用当前页面会话查询。首次登录或会话过期时在浏览器中正常处理，不需要复制 Cookie。
5. 结果写入项目 `outputs`，每次生成带类目、日期和时间戳的 JSON 与 Excel，不覆盖历史结果。网页提供本次结果、历史结果下载和打开输出目录按钮。

Excel 包含“关键词分析”和“词根归类”两张表，提供筛选与冻结表头。人数保留数值，点击率、转化率及环比按百分比显示。没有确认含义的原始字段保留原名称；新增字段也会自动保留。

## 时间规则

- 近7/14/30天：截止日由该类目接口的最新可用日期决定，不能把当前日期强行当作已有数据日期。
- 指定周：填写周内任意一天 `YYYY-MM-DD`，按网站当前实现转换为周日至周六；请求使用周六日期。界面会显示所选类目的可用周范围。
- 指定月：填写 `YYYY-MM`，按自然月查询。未发布的月份不能采集。
- 不支持任意起止日期，因为当前词根接口按固定窗口、自然周或自然月返回。程序不会把其他周期数据冒充所选周期。

## 登录资料与请求方式

参考 `D:\cmProject\dmp\dmp_zdyqrysc` 的 DrissionPage 浏览器模式：固定连接端口、Chrome 用户目录、正常登录、运行前重新读取会话。按用户指定，默认资料目录为 **`D:\ChromeDebug\User Data`**，连接端口 **9351**。如果参考项目已用该目录和端口打开 Chrome，可复用同一浏览器并打开边界BI标签页。

可以在界面指定已有 Chrome 资料目录，但该目录不能同时被另一个 Chrome 进程使用。程序不强制关闭浏览器、不复制其他浏览器资料。端口若属于其他程序或资料目录，会提示更换。

与 DMP 不同，边界BI还有动态 `access-referer` 和加密响应。因此本项目让实际浏览器调用网站当前版本的 GET/POST 模块，并用网站公开的 WASM 模块解码，Cookie 仍由 Chrome 按正常域、路径及 HttpOnly 规则发送。不会把 Cookie、Token、动态请求头写入配置、日志或结果文件。静态模块名称和导出名自动发现，网站结构变化时明确报错。

## 数据范围与失败处理

关键词目前一次返回整批列表，页面分页不需要额外抓取。达到2000条时会在日志和Excel注明可能存在服务端上限，程序不声称已经获得站点全库。

只获取所选类目的词根归类和关键词，不批量展开关联商品或趋势。第一次接口成功即保存词根JSON；之后关键词失败、停止或Excel被占用时，已有JSON仍保留。状态为 `partial` 的JSON不代表采集完成。完整JSON可在修复文件占用后单独重新导出，无需重新请求。

收到401、403、429或业务失败时停止并提示检查登录、权限、验证或稍后重试，不轮换账号、代理或高频重试。停止按钮会在等待登录期间及时生效；已发出的单次请求最长等待约60秒。关闭最后一个采集网页约10秒后自动停止本地服务，刷新网页不会退出。持续未收到页面连接时约150秒后清理，Chrome保留供用户查看。也可点击网页右上角“退出程序”。

## 命令行

```powershell
python -m pip install -r requirements.txt
python webapp.py

python collector.py --category-id 50002768 --category-name "个人护理/保健/按摩器材" --first-category-id 50002768 --period 7
python collector.py --category-id 50002768 --category-name "个人护理/保健/按摩器材" --first-category-id 50002768 --period month --date 2026-09
python collector.py --category-id 50002768 --category-name "个人护理/保健/按摩器材" --first-category-id 50002768 --period week --date 2026-10-21
```

`--profile`、`--port`、`--output-dir` 可自定义。`--dry-run` 只检查输入选择，不打开浏览器、不请求接口；实际可用日期检查发生在登录后。

从已完成的JSON重新导出：

```powershell
python -c "import json; from pathlib import Path; from excel_export import export_result; p=Path(r'outputs/你的结果.json'); export_result(json.loads(p.read_text(encoding='utf-8')),p.with_suffix('.xlsx'))"
```

网页端口可用 `python webapp.py --port 18767` 修改。服务仅监听本机回环地址；修改操作校验来源和本地会话标识。浏览器登录连接端口9351与网页端口18766是两个不同设置。

离线验证：`python -m unittest -v test_collector test_webapp`，目前22项通过。已用指定的 `D:\ChromeDebug\User Data` 完成登录会话读取、可用时间查询和2026年9月个护类目采集，得到1188个词根、1997条关键词；网页下载文件与输出Excel的内容校验一致。另已验证电吹风二级类目2026-10-18至2026-10-24的指定周查询，得到255个词根、403条关键词。类目接口返回包含一级节点的扁平树，程序按pid建立二级/三级关系，不把全部后代混成二级类目。

浏览器交互API依据：[DrissionPage页面交互](https://www.drissionpage.cn/browser_control/page_operation/)、[获取网页信息](https://www.drissionpage.cn/browser_control/get_page_info/)。
