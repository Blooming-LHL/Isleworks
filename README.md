# Isleworks

一个受《小岛经济学》启发的经济模拟实验：6 位居民在资源与时间约束下捕鱼、采集、制造渔网和交易，运行 30 个模拟日。

项目目录：`/Users/lhl/Projects/Isleworks`。设计方案见 [MVP v0.1](docs/自主经济模拟系统_MVP_v0.1.md)，实现进度见 [实施记录](docs/IMPLEMENTATION.md)。

## 本地启动

需要 Python 3.11 或更新版本；核心运行不依赖第三方库。以下命令均在项目根目录执行：

```sh
cd /Users/lhl/Projects/Isleworks
PYTHONPATH=src python3.11 -m isleworks serve
```

打开 <http://127.0.0.1:8765>，创建一个规则基线世界。像素岛屿展示居民的计划活动，可暂停、单步推进或查看历史日期；地图位置和人物移动是视觉示意。点击“数据统计”查看同一运行的食物、人口、交易和活动趋势；“计划主要活动人数”与“实际执行劳动时段”采用不同口径。岛屿页面底部展示事实报告和模型复盘。居民决策使用规则；轮次完成后，程序自动保留事实报告，并使用本机模型配置生成 LLM 复盘（每轮独立预算最多 2 次请求，可能产生费用）。页面会自动更新复盘状态、结论、证据及资源调整建议。配置缺失或复盘失败不会改变世界结果。

Windows PowerShell：

```powershell
cd <你的 Isleworks 仓库目录>
$env:PYTHONPATH = "src"
py -3.11 -m isleworks serve
```

## 命令行实验

```sh
# 默认规则基线，完整运行 30 日
PYTHONPATH=src python3.11 -m isleworks run --scenario baseline --seed 42

# 对照实验：没有渔网 / 第 15–17 日捕鱼减产
PYTHONPATH=src python3.11 -m isleworks run --scenario no-nets
PYTHONPATH=src python3.11 -m isleworks run --scenario fishing-shock

# 查看归档与逐日重放；将 RUN_ID 替换成运行时打印的 ID
PYTHONPATH=src python3.11 -m isleworks list
PYTHONPATH=src python3.11 -m isleworks show RUN_ID
PYTHONPATH=src python3.11 -m isleworks replay RUN_ID
PYTHONPATH=src python3.11 -m isleworks review RUN_ID
```

默认归档在 `runs/isleworks.sqlite3`，已加入 Git 忽略。可在子命令前通过 `--db /path/to/archive.sqlite3` 指定独立归档。重放使用已保存的意图，不再次请求模型。暂停后的实验用 `resume RUN_ID` 继续。

## DeepSeek 居民决策（v0.3）

居民只生成精简 Decision：目标、简短说明、1–3 日劳动与订单。程序绑定居民身份、日期和 ID，并检查当天确定的动作前置条件；正常计划和明确降级计划统一交给引擎执行。

新运行默认使用 DeepSeek Strict 工具 `submit_decision`。基本配置仍只有三个变量：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

首次配置时复制 `.env.example` 为 `.env`，然后用编辑器填写密钥；已有文件直接编辑，无需重新复制。PowerShell 对应命令为 `Copy-Item .env.example .env`。

```sh
cp .env.example .env
```

应用通过原有加载模块读取当前工作目录的 `.env`，请从项目根目录启动。支持 `KEY=VALUE`、可选引号、空行和整行注释，不执行变量展开。CLI > 系统环境 > `.env` > 默认值；旧 ENDPOINT 别名继续兼容。`.env` 被 Git 忽略，密钥不作为命令行参数，也不应提交或发送到聊天。

按照 [DeepSeek Strict 文档](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/#strict-模式beta)，程序把官方根地址映射到 `/beta/chat/completions`，设置 `strict: true`、关闭思考模式并指定工具。配置中不写请求后缀。官方 `/v1` 路径不用于本项目的 Strict 决策；自定义网关需要明确支持的 Beta 路由，程序不会猜测或静默切换模式。

**本次 Strict 接入完成了离线验证，真实服务验收仍需下面的显式请求。** 以下命令会通过程序使用本机配置，可能产生模型费用，网页启动的规则实验不会执行这些居民模型决策，但结束时默认会调用模型复盘：

```sh
# 建议先执行单居民 Strict 测试，最多两次请求，不推进世界、不写实验归档
ISLEWORKS_LIVE_DECISION_TEST=1 PYTHONPATH=src python3.11 -m unittest discover -s tests/isleworks -p test_model_decision.py -v

# 六居民、三日短实验；最多 18 个居民日，每个最多两次请求
PYTHONPATH=src python3.11 -m isleworks run --mode llm --allow-paid --days 3 --max-calls 36

# 完整实验示例；180 是请求预算，重试也计入，不是金额上限
PYTHONPATH=src python3.11 -m isleworks run --mode llm --allow-paid --max-calls 180

# 仅用于显式开发对照，启动新实验；运行失败不会自动切到此路径
PYTHONPATH=src python3.11 -m isleworks run --mode llm --allow-paid --days 3 --max-calls 36 --decision-output json_object
```

失败策略：

- 当天确定不合法的动作会收到具体错误与原候选反馈，结构、动作和传输共用最多两次请求，不叠加重试。
- 单个居民未得到有效计划，降级为四块休息且无订单，照常消费；其他居民继续。
- 预算耗尽后停止新请求，合法旧计划仍可复用，其余居民休息。世界不因此暂停，但可因饥饿全员退出而提前坍缩。
- 鉴权、服务能力或不确定请求导致该运行的模型通道关闭，后续需要生成计划的居民休息，不自动更换模型或增加预算。
- 账本不变量、关键持久化故障和用户主动中断仍可停止世界推进。

运行结果中的 `metrics.decision_sources` 区分模型休息块、规则休息块、失败休息块及降级居民日；观察器的居民卡片也会标明“降级休息”。已提交与待提交决策分别计数，降级不算作模型输出成功。

新实验记录协议、提示词、编译器、预检与适配版本、实际模式和路由。续跑必须匹配原设置和预算；可从已审计成功响应恢复尚未落库的决策，避免重复请求。旧 LLM 协议运行继续支持查看、诊断和离线重放，升级后的模型实验请新建。

轮次完成或经济坍缩时，先归档确定性事实报告，再自动生成 LLM 复盘，两份报告分别保存。复盘使用 JSON 输出接口，独立于居民 Strict 模式，每轮最多 2 次请求（含纠错），不随刷新页面重复调用。网页会显示生成中、已完成、失败或配置不可用，并展示有证据引用的判断、资源调整建议、假设与风险。建议不会自动应用到世界。

要完全离线运行，显式关闭自动 LLM 复盘：

```sh
PYTHONPATH=src python3.11 -m isleworks serve --no-llm-review
PYTHONPATH=src python3.11 -m isleworks run --no-llm-review
```

`review RUN_ID` 可查看事实报告；已有模型复盘会复用缓存，不重复收费。旧手动命令保留兼容，正常实验无需执行。参数建议可另做规则配对筛选：

```sh
PYTHONPATH=src python3.11 -m isleworks screen RUN_ID
```

复盘校验失败会记录具体字段路径；已移除每条判断最多 8 条证据的错误限制，所有证据仍须存在于引擎提供的集合。对于旧版本保存的完整失败响应，修复后可离线重新校验恢复，并保留原失败记录。

资源建议只做规则配对筛选，不自动采纳；真实 LLM 配对验证和正式采纳流程尚未完成。

## 查看模型失败原因

运行结果的 `metrics.failure_summary` 会汇总失败次数、错误阶段、字段路径及最近失败。查看完整的已归档诊断：

```sh
PYTHONPATH=src python3.11 -m isleworks diagnose RUN_ID

# 如需查看经过脱敏、限长的失败输出片段
PYTHONPATH=src python3.11 -m isleworks diagnose RUN_ID --include-response
```

这些命令离线读取归档，不加载 `.env`，不发起模型请求。每次失败会关联居民、模拟日、请求 ID 和重试次数，并区分网络、HTTP、响应结构、JSON 解析、动作前置检查等阶段，多项独立错误记录在 errors 列表中。字段错误示例：

```json
{
  "resident_id": "r3",
  "day": 2,
  "error_detail": {
    "stage": "validation",
    "code": "missing_field",
    "path": "$.days[0].orders[0].price",
    "message": "Required field is missing"
  }
}
```

解析和校验失败后的下一次尝试会收到具体修正反馈，总尝试次数仍最多两次，并计入原有预算。降级决策会保存失败请求的关联信息；诊断会标明该决策是否已提交到世界。失败输出片段最多 2000 字符，记录前进行密钥脱敏；默认查询不展示片段、提示词或配置。

旧归档保留原样，只能显示已有的异常类别；未记录的字段原因和输出不能事后恢复。

## 模型配置连通性测试

在根目录配置好 `.env` 后，可显式运行一次真实模型测试：

```sh
ISLEWORKS_LIVE_TEST=1 PYTHONPATH=src python3.11 -m unittest discover -s tests/isleworks -p test_model_connection.py -v
```

测试通过应用现有配置入口加载设置，不直接访问 `.env`，最多发出一次请求，限制输出为 64 token，不打印密钥、配置值或模型响应，也不写入实验归档。普通测试默认跳过该项。失败时仅报告 HTTP 状态或网络错误类别；不会关闭 TLS 校验或自动换用其他模型。

## 模块与验证

`domain` 定义数据契约；`engine` / `market` 执行经济规则；`agents` / `llm` 生成与验证计划；`storage` 保存 SQLite 归档；`analysis` / `evolution` 提供事实统计和受约束建议；`runner` 协调推进；`interfaces` 提供 CLI 与本地观察器。

食物和价格使用整数毫鱼（1000 单位 = 1 条鱼），交易和日终快照可审计。这是玩具经济模型，参数与实验结果不代表现实经济规律。

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests/isleworks -v
```

当前尚未发布到 GitHub；本次 v0.3 实现只运行离线验证，真实模型测试默认跳过。
