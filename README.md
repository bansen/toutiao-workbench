# 头条选题采集与 AI 草稿工作台

独立的本地网页工具：从头条可见页面发现素材，筛选、去重入库，生成草稿，再由人核实、编辑和导出。不会自动发布。

## 启动

需要 Python 3.11+ 和已安装的 Google Chrome。

项目仓库：[bansen/toutiao-workbench](https://github.com/bansen/toutiao-workbench)。功能范围、当前状态和后续分发方式见 [项目说明与部署方案](docs/项目说明与部署方案.md)。

首次获取源码：

```bash
git clone https://github.com/bansen/toutiao-workbench.git
```

```bash
cd toutiao-workbench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m workbench
```

访问 **http://127.0.0.1:8765/**。按 `Ctrl+C` 停止。程序固定监听本机地址；不要通过反向代理或修改启动参数对公网开放。它是单用户工具，不包含互联网账号认证。

以后启动只需：

```bash
cd toutiao-workbench
source .venv/bin/activate
python -m workbench
```

上述命令适用于 macOS / Linux 终端。使用独立虚拟环境，避免修改其他项目依赖。Windows 源码运行需自行配置 Python 和 Chrome，并使用对应的虚拟环境激活命令；目前未完成真实 Windows 环境验收。

## 模型配置

采集不要求配置模型。生成草稿前：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
LLM_BASE_URL=https://你的模型服务/v1
LLM_API_KEY=你的密钥
LLM_MODEL=模型名称
```

填写兼容 Chat Completions 的服务地址前缀，不要附加 `/chat/completions`。本工具会请求 `<Base URL>/chat/completions`，要求模型输出包含 `title`、`body`、`verify` 的 JSON 对象。修改配置后重启，点击网页“模型设置 → 测试模型连接”。测试会调用一次模型并使用少量额度。

密钥只从本地环境文件或进程环境读取，不保存到 SQLite、不回传前端、不记录到任务日志。来源正文仅在你请求生成草稿（或主动开启自动生成）后发送给所配置的模型服务。没有配置真实密钥时，生成接口会明确报错，不会使用模拟内容顶替。

## 使用流程

1. 填写关键词或点选领域。体裁、领域和表达风格支持多选，红色表示选中，再次点击取消；时间、排序、长度和版本数为单选。领域“不限”会清除其他领域选择。
2. 多选体裁和领域时，分别搜索各个组合，共用每轮检查上限，候选额度均分；上限必须足够覆盖所选组合，重复素材只保留一条。点击“开始采集”，也可逐行导入头条链接。
3. 素材表显示实际读取的数据；点击标题查看保存的正文，点击箭头打开原始页面。
4. 勾选素材。单篇改写为每篇分别生成；多来源成稿需选 2～5 篇相关素材。每种所选风格独立生成 1～3 个版本，例如 2 种风格 × 2 版会生成 4 篇草稿。
5. 在草稿箱核实来源、编辑正文和待核实事项，保存、复制或导出 Markdown / 纯文本。

如果公开页面要求登录，点击“打开登录窗口”，在**工具专用 Chrome** 中手动登录。工具不会读取你日常 Chrome 的资料目录，也不自动填验证码。页顶“检查会话”可查看状态、清除专用会话。浏览器启动状态不能证明账号已登录，仍应在窗口中确认。

采集遇到验证码、限流或明确的登录要求时暂停。处理后点击“恢复采集”。如果浏览器关闭或页面改版，任务记录会展示失败原因，可打开登录窗口或手动重试。

## 定时与重试

- 定时默认关闭，支持 30 / 60 / 180 分钟，保存时记录当前采集条件。
- 开启“新增素材自动成稿”后，每轮最多处理 3 篇新增素材，只支持单篇模式。
- 所有任务串行执行，模型调用也单并发。上一轮未结束时跳过本次定时触发，不积压补跑。
- 电脑休眠、退出程序时不会执行。重启后从下一完整周期开始；上次未完成任务标为失败，需手动重试。
- 重试同一生成任务会跳过已成功版本，保留人工编辑结果。重新点击“生成草稿”则创建新任务，会重新调用模型。
- 模型超时、HTTP 429 或服务端错误最多重试两次（总共三次）。其他错误或无效 / 空响应明确失败，不写入假草稿。

## 数据与备份

数据默认位于项目的 `data/`：

| 内容 | 位置 |
|---|---|
| 素材、草稿、任务、设置 | `data/workbench.db` |
| 专用 Chrome 会话 | `data/browser/` |
| 采集验证记录 | `data/probe*/probe.json` |
| 本地密钥 | `.env` |

退出程序后备份 `data/workbench.db`。清除浏览器会话不影响素材和草稿。恢复数据库时先停止工具。`.env`、整个 `data/` 和浏览器测试截图已加入 `.gitignore`。

## 已验证能力与边界

见 [采集验证报告](docs/采集验证报告.md) 和 [逐条样本](docs/probe-evidence.json)。网页会根据 `docs/capabilities.json` 标识已支持的体裁和指标。

- 使用 Playwright 读取渲染后的 DOM，不调用头条私有 API，不实现接口签名逆向。
- 阅读量当前未从页面验证可读，相应条件禁用；不会以评论量替代阅读量。
- 文章点赞量未验证可读；微头条选择点赞排序时，仅在搜索卡片明确显示数字时记录，其他布局仍为未知。
- 评论量只在搜索卡片明确显示数字时记录。没有显示时是 `null`，不是 `0`。
- 时间范围基于可读发布时间；启用时间筛选后，缺失时间的候选会排除并计数。
- 阅读量区间为左闭右开；列表按已知指标降序，未知排在末尾。
- 每轮检查上限不是保证采集数。只处理实际发现的结果，最多尝试 4 页；网页没有可跟随的下一页时提前结束。
- 图片不下载、无 OCR。图文微头条只采集可见文字，图片里的文字不计入正文。
- 本地只存储资料和草稿，不证明内容原创，也不承诺平台推荐或收益。

## 验证与开发

自动化测试（不使用真实账号或真实模型额度）：

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

浏览器集成测试（临时数据库、模拟模型、仅监听 `127.0.0.1:8766`）：

```bash
python tests/browser_smoke.py
```

覆盖搜索卡片延迟加载、验证弹层、多选与取消、设置保存回显、多风格多版本、多来源、编辑保存、复制、MD/TXT 下载和数据库重开。测试内容明确标注为测试，不写入正式数据库。截图保存到 `output/playwright/`。

真实数据源验证：

```bash
python -m workbench.probe --keyword AI
# 只验证微头条，或打开可见浏览器
python -m workbench.probe --kind micro --visible --output data/probe-micro
```

验证默认使用 `data/browser/`；运行前请停止工作台，避免占用同一 Chrome 配置目录。可使用 `--profile data/probe-browser` 指定独立目录。验证输出不会自动改变能力开关，需要先核对样本。首次验证的 9 篇搜索文章另补抽了 1 篇首页文章；详情见报告。

## 本地 API

修改请求必须携带与工作台相同的 `Origin` 和 `X-Workbench-Request: 1`；跨站请求和异常 Host 被拒绝。请求字段采用严格验证，额外字段会报错。

| 功能 | 接口 |
|---|---|
| 状态、能力、模型配置状态 | `GET /api/status` |
| 打开 / 检查 / 清除会话 | `POST /api/session/open`、`GET /api/session`、`POST /api/session/clear` |
| 采集 / 导入 / 生成任务 | `POST /api/tasks/collect`、`POST /api/tasks/import`、`POST /api/tasks/generate` |
| 任务记录 / 停止 / 重试 | `GET /api/tasks`、`POST /api/tasks/{id}/stop`、`POST /api/tasks/{id}/retry` |
| 恢复采集 / 定时 | `POST /api/tasks/resume`、`PUT /api/schedule` |
| 素材列表 / 原文 | `GET /api/materials`、`GET /api/materials/{id}` |
| 草稿列表 / 查询 / 编辑 / 导出 | `GET /api/drafts`、`GET/PUT /api/drafts/{id}`、`GET /api/drafts/{id}/export?format=md` |
| 保存偏好 / 模型连接测试 | `POST /api/preferences`、`POST /api/model/test` |

网页入口和业务 API 独立；采集器、模型适配器、任务引擎和 SQLite 存储分别位于 `workbench/` 对应模块中。

采集请求新增 `kinds`、`domains` 数组，生成请求新增 `styles` 数组；仍兼容旧的 `kind`、`domain`、`style` 单值字段和已保存设置。多选领域之间为分别检索，而不是把所有领域拼成一个关键词。
