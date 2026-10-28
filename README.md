# 脚本工具中心

在每个人自己的 Windows 电脑运行，用自己的账号登录，通过同一个网页使用、管理和更新采集脚本。

## 源码与发布包

本仓库的 **Code** 页面管理源码、提交记录和开发说明；**Releases** 用于分发安装包、签名更新包、更新清单及版本说明。提交源码不会自动发布更新包，使用者仍通过平台的版本更新功能安装正式发布的版本。

从仓库克隆或下载源码后，安装 Python 和 Chrome，双击 `启动工具中心.cmd` 即可启动。使用拼多多工具还需安装支持 SQLite 的 Node.js 22 或以上版本及附带 npm。首次运行由 `initial-versions.json` 建立本机版本清单，默认启动当前源码版本；已有安装继续使用自己的版本记录，不覆盖本机设置。

源码不包含账号、Cookie、浏览器资料、个人配置、采集结果、运行环境或发布私钥。`state/`、`outputs/`、`runtime/`、`dist/` 等目录只保存在本机。`trust/release-public-key.pem` 为可公开的更新验证公钥。

## 启动与使用

1. 安装 Python 3.10 或更高版本及 Google Chrome。
2. 双击 `启动工具中心.cmd`，自动打开 `http://127.0.0.1:18760`。首次运行会安装组件，需要能访问 pypi.org。
3. 在工具库选择所需工具，设置类目、时间等查询条件，加入队列。
4. 每次采集自动打开 Chrome 并获取当前登录状态。尚未登录时，在 Chrome 窗口中正常完成登录或验证。
5. 在任务中心查看日志、停止任务；在结果文件中下载 Excel、原始 JSON 或打开结果文件夹。

所有工具共用平台配置的 Chrome 用户目录和连接端口，具体规则见下方“共享浏览器”。原有独立脚本在工具中心之外运行时，请避免同时采集。

关闭网页后，后台服务和任务继续运行。再次双击启动文件会打开已有服务。完成工作后，可在偏好设置中退出工具中心；运行中需要先停止任务并等待当前请求结束。

## 共享浏览器

**平台内所有脚本，包括后续新增脚本，统一使用同一个 Chrome 用户目录和连接端口，不再为单个工具创建独立浏览器目录。**

| 工具 | 默认 Chrome 用户目录 | 默认连接端口 |
| --- | --- | --- |
| 边界 BI · 词根分析 | `D:\ChromeDebug\User Data` | `9351` |
| 达摩盘 · 自定义圈人 | `D:\ChromeDebug\User Data` | `9351` |
| 拼多多 · 评论采集 | `D:\ChromeDebug\User Data` | `9351` |

目录和端口在工具中心“偏好设置”中统一修改，所有工具读取平台配置，不能各自写死另一套配置。修改前需要等待所有采集任务结束。

首次使用或浏览器目录配置为空时，默认使用 `D:\ChromeDebug\User Data`。目录不存在时，平台自动创建该目录及缺失的父目录，无需手动新建；已有目录及其中的登录资料保持不变。设置其他目录时也会自动创建。若盘符不存在或没有写入权限，平台会显示原因，可在“偏好设置”中修改目录，不会悄悄切换到另一套浏览器资料。

- 每位使用者在自己的 Windows 电脑使用自己的浏览器资料；不同电脑、不同使用者之间不共享账号。各网站仍需分别正常登录，登录状态保存在上述同一目录内。
- 已启动的共享 Chrome 会被复用。采集操作互相等待，避免同时争用浏览器。
- 停止单个工具只停止该工具的任务或断开连接，不得关闭共享 Chrome 或其他工具的标签页。
- 拼多多旧版的 `state/tools/pdd-review/browser-profile-chrome` 目录保留但不再使用，不会自动复制其中的登录资料。若共享 Chrome 中尚未登录拼多多，请正常登录一次。
- 浏览器资料保存在代码版本目录之外，不随工具更新删除，也不打入安装包或上传 GitHub。

## 已接入的工具

- **拼多多 · 评论采集**：保留原页面和采集参数，暂不使用钉钉登录；在共享 Chrome 正常登录拼多多并手选商品。Excel 仍为“用户名称、商品型号、评论”三列，下载时同步归档至 `outputs/`。刷新或切换工具会保留记录，仅手动清空时删除。原项目 `D:\cmProject\pdd\pdd_pl` 未修改。首次使用需 Node.js 22 或以上版本及附带 npm，运行组件自动安装。

- **边界 BI · 词根分析**：一级、二级、三级类目；最近 7/14/30 天、自然周、自然月；同步可用日期；生成词根与关键词两张 Excel 表及原始 JSON。自然周为周日至周六；滚动周期以网站最新可用日期为准。网站若限制关键词返回数量，工具会在日志与 Excel 中提示。
- **达摩盘 · 自定义圈人**：直接保留原项目的页面及业务逻辑：一级汇总、二级/三级明细勾选、多栏类目列表、已选类目金额编辑表、金额模板、常用组合、导出文件名、任务预览弹窗、停止和续采。只通过独立适配层接入工具中心，并统一颜色与字体；原项目未修改。Excel 继续采用每类目一个工作表、月份横排、金额区间纵排的原格式。

历史边界 BI 结果仍在 `outputs`，会显示在结果中心。所有工具的新 Excel 和 JSON 导出都直接存放在 `outputs/`，不再按工具或任务分子文件夹。文件名统一加上工具标识、时间和唯一编号，同名结果也会分别保留。达摩盘仍保留原页面的文件名输入与 Excel 格式，统一归档时追加上述标识。采集工作区和续采进度放在 `state/`；旧版结果保留原位置，原有链接和历史续采仍可使用。停止或失败时，已保存数据保留；DMP 可在原页面点击“继续未完成采集”。切换工具页面不会结束后台采集，由工具中心统一管理任务。

## 统一输出文件夹

默认位置：`D:\cmProject\脚本工具中心\outputs`（安装在其他位置时，为该安装目录下的 `outputs`）。目录不存在时自动创建。边界 BI、达摩盘、拼多多及后续新增脚本的所有新导出都直接保存到此文件夹，可在“结果文件”统一查看、下载，或点击“打开结果文件夹”。

中间文件和当前采集槽位放在 `state/` 内部工作区；清空某个工具当前记录不会删除统一归档的历史结果。旧版历史子目录保留原样，不删除已有文件。

## 更新

在偏好设置填写公开 GitHub 仓库，例如 `账号名/脚本发布仓库`，在版本更新中点击检查。客户端通过 [GitHub Releases API](https://docs.github.com/en/rest/releases/releases) 读取最新正式 Release 的签名 `updates.json`，下载对应附件，校验签名、SHA-256、最低工具中心版本及启动入口。

也可直接选择开发者提供的签名 ZIP 包离线安装。安装或回退前需要等待任务队列结束。每个工具独立保存代码版本及 Python / Node 环境，配置、浏览器资料、任务记录和结果存放在版本目录之外。

安装采用“暂存 → 验证 → 独立环境 → 启动检查 → 原子切换版本”。检查失败保留当前版本；已更新工具可恢复上一版本。工具中心自身更新后点击重启；新版本服务启动失败时，启动器会尝试恢复上一版本。

首次安装包只包含程序、公共类目缓存及发行公钥，不包含个人配置、浏览器资料、任务记录、采集数据或发行私钥。GitHub 更新当前使用公开 Releases，无需向使用者分发 GitHub 访问令牌。

## 开发者发布

在本目录使用 PowerShell：

```powershell
# 首次构建：生成发行密钥、全部已接入组件包、更新清单及全新安装包
.\.venv\Scripts\python.exe publisher\release.py build --distribution

# 修改一个工具后发布新版本
.\.venv\Scripts\python.exe publisher\release.py build --tool boundary --version 1.0.1 --notes "本次更新说明"

# 工具中心自身升级
.\.venv\Scripts\python.exe publisher\release.py build --tool hub --version 1.0.1
```

产物位于 `dist/`。同一输出目录会保留其他工具最新的清单条目。创建新的 GitHub Release 时，上传 `updates.json` 以及其中列出的所有附件，另上传首次安装包与 `SHA256SUMS.txt` 供新用户下载。请勿遗漏清单中未变更工具的附件。正式发布后不修改同版本附件，继续发布更高版本号。

发行私钥位于 `%LOCALAPPDATA%\ScriptToolCenterPublisher\signing-key.pem`，独立于项目目录。后续更新沿用这一密钥；公钥随首次安装包分发。将首次安装 ZIP 发给使用者，解压后双击启动文件；首次安装包仍需要本机 Python 和 Chrome；拼多多工具还需要 Node.js 22 或以上版本。

## 增加新脚本

新工具使用独立目录，提供 `adapter.py`、`tool.json`、`requirements.txt`。适配程序通过 `tool_sdk.Context` 取得参数、用户数据目录、输出目录、停止信号，以及日志和进度桥接。`--health-check` 必须只检查依赖和入口，不执行真实业务。

### 新脚本必须遵守的浏览器规则

1. 从平台读取共享浏览器的 `profile` 和 `port`，默认值为 `D:\ChromeDebug\User Data`、`9351`；用户修改平台设置后，脚本必须跟随配置。
   连接前确保目录存在：配置为空时回填默认目录，目录缺失时自动创建父目录及目录本身；创建失败应提示原因，不得自行改用独立目录。
2. 复用同一个 Chrome 实例。Python 适配程序在连接前调用 `ctx.verify_browser()`；Node 工具通过平台验证后的本机连接地址接入。端口被其他目录或程序占用时，提示用户处理，不强行关闭或接管。
3. 不自行创建 `browser-profile`、`browser-profile-chrome` 等独立用户目录，也不另开使用相同目录的第二个 Chrome 进程。
4. 接入平台的采集互斥机制；停止时只清理本工具的任务和连接，不调用关闭整个共享浏览器的操作，也不关闭其他工具的页面。

所有新脚本必须通过平台归档功能将最终 Excel / JSON 直接保存到同一个 `outputs/` 文件夹，文件名避免覆盖；工作文件和续采数据放在 `state/`。Python 工具完成后由任务队列自动归档，网页工具通过平台 `Store.save_export` 归档。

`tool.json` 支持标准参数表单，新增工具无须修改中心页面：

```json
{
  "id": "my-tool",
  "name": "我的工具",
  "description": "说明这个脚本能完成什么工作",
  "version": "1.0.0",
  "min_hub_version": "1.0.0",
  "entry": "adapter.py",
  "actions": ["collect"],
  "form": {"fields": [
    {"key": "start_date", "label": "开始日期", "type": "date", "required": true},
    {"key": "keyword", "label": "关键词", "type": "text"}
  ]}
}
```

表单类型：`text`、`number`、`date`、`month`、`textarea`、`lines`、`checkbox`、`select`。打包后在网页导入签名包，或随下一次 Release 分发：

```powershell
.\.venv\Scripts\python.exe publisher\release.py build --tool my-tool --version 1.0.0 --source D:\开发中的工具目录
```

## 目录

```text
启动工具中心.cmd        使用者入口
launcher.py             稳定启动器与启动失败回退
app/versions/           工具中心各版本
 tools/<编号>/versions/ 脚本各版本
runtime/envs/           Python 脚本、各版本独立运行环境
runtime/node-envs/      Node 脚本、各版本独立运行环境
state/                  本机配置、类目缓存、任务数据库
outputs/                所有采集结果
trust/                  发行公钥
publisher/              开发者打包工具
dist/                   可分发的签名发行包
analysis/               开发验收资料，不随安装包分发
```

根目录旧 `collector.py`、`webapp.py` 等是迁移前的兼容源码；统一入口实际使用 `tools/boundary/versions/` 的版本。原采集说明备份于 `docs/原边界BI采集说明.md`。

## 验证

开发测试前，在本机开发环境安装 `requirements-dev.txt` 中的依赖。Node 工具测试还需要本机 Node.js 和 npm；测试使用独立数据目录，不采集网站真实数据。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest test_collector test_webapp -v
$env:HUB_TEST_VERSION="1.0.5"
.\.venv\Scripts\python.exe -m unittest test_unified_outputs test_browser_directory test_hub test_dmp_portal test_pdd_portal test_shared_browser -v
```

服务只监听本机地址，写请求校验来源和页面令牌。浏览器登录状态不写入发行包或采集 JSON；遇到登录失效、验证或权限问题，按站点正常流程处理。

