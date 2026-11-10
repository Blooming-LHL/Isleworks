# 技术债与待验收清单

核对日期：2026-11-10。已结束批次的过程与测试结果见 [历史实施记录](completed/IMPLEMENTATION.md)。本清单区分已核实的实现限制与仍需核实的验收缺口，不自动将历史待办视为当前缺陷。

| 编号 | 状态 | 事项与完成条件 | 证据 |
| --- | --- | --- | --- |
| TD-01 | 待实现 | 资源提案当前仅支持规则基线配对筛选。补齐单独预算的真实 LLM 配对验证、明确采纳与下一轮配置衔接后，再验收完整迭代链路。 | [Runner.screen](../../src/isleworks/runner/__init__.py)、[演化设计](../product-specs/自主经济模拟系统_MVP_v0.1.md) |
| TD-02 | 当前限制 | 模型预算限制请求次数、输入字符与输出 token，尚无按金额计算的硬上限。若需要金额预算，应先确定模型价格与实际用量的核算规则。 | [模型网关](../../src/isleworks/llm/__init__.py) |
| TD-03 | 待优化与验收 | 统计仍逐日解析、校验截至所选日的快照和事件，尚无按日预聚合。10,000 居民仅有合成展示测试；需另外验证大规模模拟、统计读取性能与模型成本。 | [Store.statistics_archive](../../src/isleworks/storage/__init__.py)、[像素观察与统计批次记录](completed/IMPLEMENTATION.md#像素观察与数据统计2026-11-10) |
| V-01 | 待核实验收证据 | 历史记录未包含真实 Strict 单居民与真实 LLM 30 日验收结论。需核对已有归档并补齐证据；本次文档整理未发起真实模型调用。 | [历史验证与待验收记录](completed/IMPLEMENTATION.md)、[单居民模型测试](../../tests/isleworks/test_model_decision.py) |
| V-02 | 待核查 | 历史记录提及强制终止后遗留的 running 状态与安全恢复指令问题。需针对当前实现复现、核查，再确定修复范围。 | [历史待验收记录](completed/IMPLEMENTATION.md)、[运行控制](../../src/isleworks/runner/__init__.py) |

自动轮次 LLM 复盘已有对应实现，不再作为未实现项；实现与离线验证证据见 [自动轮次复盘调整](completed/IMPLEMENTATION.md#自动轮次复盘调整2026-11-08)。历史的“未发布到 GitHub”等描述保留为当时记录，不据此新增当前待办。

[返回执行计划索引](index.md)
