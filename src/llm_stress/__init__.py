# -*- coding: utf-8 -*-
"""llm-stress：面向 LLM 推理服务的压测工具。

设计口径（与 README「指标定义」一节一一对应）：
  TTFT   req 发出 → 首个内容 token 到达
  TPOT   (total_ms - ttft_ms) / max(out_tokens - 1, 1)
  吞吐   单位时间内成功请求产出的 output token 数
  分位   线性插值（与 numpy.percentile 默认一致），不做近邻取序
"""

__version__ = "0.1.0"
