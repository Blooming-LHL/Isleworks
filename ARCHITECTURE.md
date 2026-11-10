# Isleworks 架构地图

## 系统边界

Isleworks 是本地运行的经济模拟项目：按配置创建岛屿，以居民计划驱动每日资源变动、市场交易和事件记录，再通过命令行或本地网页观察归档。当前默认配置为 6 名居民、30 天；食物及价格使用整数毫鱼，1,000 毫鱼等于 1 条鱼。项目的核心运行要求 Python 3.11 或更高版本，不声明第三方运行依赖。入口和配置见 [CLI](src/isleworks/interfaces/cli.py)、[领域模型](src/isleworks/domain/__init__.py)、[构建配置](pyproject.toml)。

## 模块与入口

| 模块 | 职责 | 入口 |
| --- | --- | --- |
| 领域模型 | 场景、居民、计划、事件和世界状态的数据结构及校验 | [domain](src/isleworks/domain/__init__.py) |
| 决策与模型 | 规则计划、模型决策编译和模型网关 | [agents](src/isleworks/agents/__init__.py)、[decision](src/isleworks/agents/decision.py)、[llm](src/isleworks/llm/__init__.py) |
| 世界与市场 | 根据意图计算一天的经济行为、撮合交易并产生事件 | [engine](src/isleworks/engine/__init__.py)、[ledger](src/isleworks/engine/ledger.py)、[market](src/isleworks/market/__init__.py) |
| 运行协调 | 创建、推进、重放和复盘一次运行 | [runner](src/isleworks/runner/__init__.py) |
| 归档 | 使用 SQLite 保存配置、运行、每日快照、决策、调用和报告 | [storage](src/isleworks/storage/__init__.py) |
| 统计与复盘 | 从事件生成事实指标，校验模型复盘和参数建议 | [analysis](src/isleworks/analysis/__init__.py)、[evolution](src/isleworks/evolution/__init__.py) |
| 用户接口 | 命令行入口、本地 HTTP 观察器、像素岛屿与统计页面 | [CLI](src/isleworks/interfaces/cli.py)、[server](src/isleworks/interfaces/server.py)、[网页入口](src/isleworks/interfaces/web/index.html)、[统计页](src/isleworks/interfaces/web/statistics.html) |

## 关键链路

### 创建与推进运行

[CLI](src/isleworks/interfaces/cli.py) 或 [网页服务](src/isleworks/interfaces/server.py) 创建运行；[Runner](src/isleworks/runner/__init__.py) 协调居民决策和世界推进；[引擎](src/isleworks/engine/__init__.py) 生成每日状态与事件并通过 [Store](src/isleworks/storage/__init__.py) 归档。运行的测试入口包括 [引擎测试](tests/isleworks/test_engine.py) 与 [验收测试](tests/isleworks/test_acceptance.py)。

### 观察、统计与复盘

本地网页服务读取归档，观察接口返回有限数量的居民代表并支持按 ID 查询；统计由 [analysis](src/isleworks/analysis/__init__.py) 从已提交数据计算。轮次结束后 [Runner](src/isleworks/runner/__init__.py) 保存事实报告，并按运行配置触发 [模型复盘](src/isleworks/evolution/__init__.py)；模型建议不直接改变世界配置。相关验证见 [统计测试](tests/isleworks/test_statistics.py)、[观察器规模测试](tests/isleworks/test_observer_scale.py) 和 [自动复盘测试](tests/isleworks/test_auto_review.py)。

## 配置与限制

- 本地启动、离线模式、模型配置与诊断命令以 [README](README.md) 为入口。
- 当前观察器的像素坐标和移动是展示示意；它们不是经济引擎中的真实地理路径。参见 [实施记录](docs/exec-plans/completed/IMPLEMENTATION.md)。
- 默认规则基线和模型辅助运行的含义、已知限制与实验验证状态，分别参见 [README](README.md) 和 [实施记录](docs/exec-plans/completed/IMPLEMENTATION.md)。
- 后续设计与已实现行为需要区分；方案入口见 [文档索引](docs/index.md)。
