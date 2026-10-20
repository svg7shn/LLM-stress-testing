# -*- coding: utf-8 -*-
"""测试数据集。

压测的价值取决于输入分布，不是请求数。固定一句"你好"只能测出最短路径，
拿到的是偏乐观的 TTFT 与偏低的 token 吞吐。这里内置四组原创样本：

  short   约 20~60 字，考察首 token 链路与调度开销
  medium  约 300~600 字，考察常规对话
  long    约 2500~4000 字（≈1800~2800 token），考察 prefill 与 KV Cache
  mixed   按 5:3:2 混合，用于长稳与容量测试

也支持外部 JSONL：每行 {"id","prompt","max_tokens","label"}。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class Sample:
    id: str
    label: str
    prompt: str
    max_tokens: int = 256
    family: str = "short"

    @property
    def input_chars(self) -> int:
        return len(self.prompt)

    @property
    def est_input_tokens(self) -> int:
        return estimate_tokens(self.prompt)


def estimate_tokens(text: str) -> int:
    """粗略估算：中文 1 字≈0.65 token，ASCII 4 字符≈1 token。仅用于界面提示。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.65 + other / 4.0) + 1


# ---------------------------------------------------------------- 内置样本
_SHORT = [
    ("s1", "一句话解释什么是 KV Cache"),
    ("s2", "把下面这句话翻译成英文：今天服务器负载很高。"),
    ("s3", "用 3 个要点说明为什么首 token 延迟比总延迟更重要。"),
    ("s4", "写一个 Python 函数，判断字符串是否为回文。"),
    ("s5", "解释 HTTP 429 和 503 的区别。"),
    ("s6", "给出三条降低推理服务尾延迟的建议。"),
    ("s7", "什么是 P99 延迟？为什么压测要看分位数？"),
    ("s8", "用一句话说明并发数与吞吐量的关系。"),
]

_MEDIUM = [
    ("m1", "我们是一个 SaaS 团队，最近把自建推理服务迁到了托管 API。请分析：在日均 20 万次调用、"
           "平均输入 800 token、输出 300 token 的场景下，应该重点监控哪些指标？请给出指标定义、"
           "采样频率、告警阈值建议，并说明每个阈值背后的取舍。"),
    ("m2", "请对比两种限流策略：令牌桶与并发信号量。分别说明它们对 P99 延迟、错误率、"
           "资源利用率的影响；如果服务端在 429 响应里带 Retry-After，客户端应该如何退避才能"
           "既不打爆上游又不浪费容量？请给出伪代码。"),
    ("m3", "我们的网关在高峰期出现：TTFT 正常但总耗时翻倍。请列出至少 5 种可能原因，"
           "按排查成本从低到高排序，并给出每种原因的验证方法（具体命令或观测指标）。"),
    ("m4", "请设计一套 LLM 服务的容量评估方案：给定 SLO（P95 首 token ≤ 1.2s，成功率 ≥ 99.5%），"
           "如何通过阶梯压测找到最大可用并发？需要多少轮、每轮多长时间、如何判断拐点？"),
    ("m5", "解释为什么「提高并发」在某个点之后反而降低吞吐。请从批处理调度、显存带宽、"
           "KV Cache 占用三个角度说明，并指出在监控面板上分别对应什么曲线特征。"),
    ("m6", "我们准备做一次 8 小时长稳测试。请给出测试计划：并发水平怎么定、数据集怎么配比、"
           "需要采集哪些时间序列、如何判定「内存泄漏」与「服务劣化」、出现异常时如何保留现场。"),
    ("m7", "请说明流式（SSE）与非流式响应在压测口径上的差异：二者测出的总耗时、token 吞吐、"
           "错误分类有哪些不可比之处？如果必须混用两批历史数据，应该做哪些归一化。"),
    ("m8", "写一份容量决策模板：输入是阶梯压测结果（并发、RPS、P95、成功率、429 比例），"
           "输出是「建议线上并发上限」「需要扩容的信号」「需要限流的信号」。要求每条规则都有明确的"
           "数值条件和例外说明。"),
]


def _long_prompt(topic: str, filler: list[str], target_chars: int = 3000) -> str:
    head = (
        f"以下是我们{ topic }相关的内部资料，请通读后回答问题。\n\n"
    )
    body: list[str] = []
    i = 0
    while sum(len(x) for x in body) < target_chars:
        body.append(filler[i % len(filler)])
        i += 1
    tail = (
        "\n\n请基于以上资料回答：如果要保证首 token 延迟稳定在 1 秒以内，"
        "在并发翻倍的情况下最可能先出现瓶颈的环节是什么？请说明判断依据，"
        "并给出三条可执行的验证步骤。"
    )
    return head + "\n\n".join(body) + tail


_FILLER_A = [
    "【接口约定】所有请求必须携带 Authorization 头，网关按 key 的哈希做分组限流，"
    "单 key 默认 300 次/分钟、并发上限 20。超过并发上限的请求立即返回 429，"
    "并在 Retry-After 中给出建议等待秒数；超过频率上限的请求进入排队，"
    "排队超过 10 秒后返回 503。",
    "【容量水位】推理集群按 GPU 显存水位分三档：绿色低于 70%，黄色 70%~85%，红色高于 85%。"
    "黄色档位会主动降低单次请求的最大输出 token 数，红色档位会拒绝新的长上下文请求。"
    "水位统计以 10 秒为窗口取中位数，避免瞬时尖峰触发抖动。",
    "【观测口径】首 token 延迟从请求到达网关开始计时，到第一个非空 delta 为止；"
    "总耗时才包含完整的流式输出。两者都要记录 P50/P90/P95/P99，"
    "只报平均值会掩盖排队造成的长尾。",
    "【故障处置】当上游返回 5xx 时，客户端按指数退避重试，最多 3 次，"
    "退避基准 500 毫秒、上限 8 秒。重试请求不计入成功数，但必须计入总请求数，"
    "否则成功率会被系统性高估。",
    "【成本核算】费用按输入 token 与输出 token 分别计价，缓存命中的输入 token 按折扣价计算。"
    "压测期间的消耗同样计费，长稳测试前必须先估算总 token 量并确认预算。",
    "【灰度规则】新模型先接入 5% 流量，观察 24 小时；期间任何 5xx 比例超过 0.5% "
    "或 P99 延迟超过基线的 1.5 倍，立即回滚。灰度流量与正式流量使用独立的限流配额，"
    "避免灰度把正式容量挤占。",
]

_FILLER_B = [
    "【数据分片】历史会话按月分片存储，单分片上限 2000 万行。查询时先按时间范围裁剪分区，"
    "再按会话 ID 做哈希路由。跨分片聚合查询需要走离线任务，不允许在线直接扫描全表。",
    "【缓存策略】热点提示词模板缓存 30 分钟，模型元信息缓存 5 分钟，"
    "鉴权结果缓存 60 秒。缓存击穿时用单飞模式合并并发回源，回源超时 2 秒。",
    "【链路追踪】每个请求生成 trace_id，贯穿网关、调度、推理三段。"
    "调度段的排队时长单独打点，便于区分「服务慢」与「排队久」。",
    "【压测隔离】压测流量必须打标签，走独立配额与独立告警通道，"
    "禁止与生产流量共用同一个 key，否则限流数据无法归因。",
]


def _build_long() -> list[Sample]:
    return [
        Sample("l1", "长上下文：接口与容量规范", _long_prompt("接口与容量规范", _FILLER_A, 3000), 512, "long"),
        Sample("l2", "长上下文：存储与缓存策略", _long_prompt("存储与缓存策略", _FILLER_B, 3000), 512, "long"),
        Sample("l3", "长上下文：混合运维手册", _long_prompt("运维手册", _FILLER_A + _FILLER_B, 3800), 512, "long"),
    ]


def _build(family: str) -> list[Sample]:
    if family == "short":
        return [Sample(i, l, l, 256, "short") for i, l in _SHORT]
    if family == "medium":
        return [Sample(i, l[:24], l, 384, "medium") for i, l in _MEDIUM]
    if family == "long":
        return _build_long()
    raise KeyError(family)


def builtin(family: str = "mixed") -> list[Sample]:
    """返回内置数据集。mixed = short 5 : medium 3 : long 2。"""
    if family == "mixed":
        return _build("short") + _build("medium") + _build("long")
    return _build(family)


BUILTIN_NAMES = ("short", "medium", "long", "mixed")


def load_jsonl(path: str | Path) -> list[Sample]:
    items: list[Sample] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
            prompt = str(obj.get("prompt") or obj.get("text") or "").strip()
            if not prompt:
                raise ValueError(f"{path}:{lineno} 缺少 prompt 字段")
            items.append(Sample(
                id=str(obj.get("id") or f"row{lineno}"),
                label=str(obj.get("label") or f"row{lineno}"),
                prompt=prompt,
                max_tokens=int(obj.get("max_tokens") or 256),
                family=str(obj.get("family") or "custom"),
            ))
    if not items:
        raise ValueError(f"{path} 没有任何样本")
    return items


def resolve(spec: str) -> list[Sample]:
    """spec 可以是内置名（short/medium/long/mixed）或 JSONL 路径。"""
    if spec in BUILTIN_NAMES:
        return builtin(spec)
    p = Path(spec)
    if p.is_file():
        return load_jsonl(p)
    raise FileNotFoundError(
        f"数据集 {spec!r} 不存在。可用内置：{', '.join(BUILTIN_NAMES)}；也可传 JSONL 文件路径。"
    )


def describe() -> list[dict[str, object]]:
    out = []
    for name in BUILTIN_NAMES:
        items = builtin(name)
        out.append({
            "name": name,
            "samples": len(items),
            "avg_input_chars": round(sum(s.input_chars for s in items) / len(items)),
            "est_input_tokens": sum(s.est_input_tokens for s in items),
        })
    return out
