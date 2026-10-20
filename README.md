# LLM 压测（llm-stress）

把并发、延迟、RPS、TTFT 与吞吐，变成可复现的服务决策。

面向 **OpenAI / Anthropic / Gemini 兼容接口** 的黑盒压测工具：只依赖 HTTP 接口本身，
不需要登录服务器、不需要读 Prometheus，也没有服务端权限要求。跑完之后给出的不是一堆
数字，而是一句话结论、一个推荐并发、一条容量上限和下一步动作。

```
可以承载 · 推荐并发 2 · 峰值 106.2 token/s
容量上限落在并发 2 与 4 之间：最高档 4 的成功率只有 33.33%，低于 99%
```

---

## 1. 快速开始

项目使用 src 布局，无需安装即可运行（把 `src` 加入 `PYTHONPATH`）。

PowerShell：

```powershell
cd LLM-stress-testing     # 换成你克隆/解压后的目录
$env:PYTHONPATH = "$PWD\src"
python -m llm_stress --help          # 顶层命令
python -m llm_stress run --help      # 全部压测参数
```

CMD / 双击：直接使用项目根目录的 `llm-stress.cmd`（已内置 PYTHONPATH，会自动找
`.venv` → `python` → `py -3`）。

```bat
llm-stress.cmd                      :: 双击等价：直接启动 Web 界面 http://127.0.0.1:8021
llm-stress.cmd run -u https://your-gateway/v1 -k sk-xxx -m gpt-4o-mini -s ladder -c 16
llm-stress.cmd models -u https://your-gateway/v1 -k sk-xxx
llm-stress.cmd serve --port 8022
```

行为约定：**不带参数 = 启动 Web 界面并自动打开浏览器**（窗口会一直开着，关掉窗口即停服务）；
带参数 = 原样透传给 CLI。命令失败时窗口会停住并显示退出码，按任意键才关闭，不会再闪退。
找不到 Python 时会提示安装，也不会一闪而过。

### 一次典型压测

```powershell
python -m llm_stress run `
  --base-url https://your-gateway/v1 `
  --api-key  sk-xxx `
  --model    gpt-4o-mini `
  --protocol openai `
  --scene    ladder `
  --dataset  mixed `
  --start 1 --stop 16 --step 2 --requests-per-stage 30
```

结束后终端直接打印结论，并在 `reports/` 下留下三个文件：

| 文件 | 内容 |
| --- | --- |
| `<run_id>.md` | 人读的报告（结论、档位表、拐点、错误分布、口径、复现命令） |
| `<run_id>.json` | 机读的结构化结果（含 decision 与全部 StageStats） |
| `<run_id>.samples.jsonl` | 每条请求一条样本，报告里的任何数字都能由它复算 |

### 一次多模型横评

同一个网关里挂了好几个模型（中转站常见），想知道哪个能用、各自能扛多少并发：

```powershell
python -m llm_stress models -u https://your-gateway/v1 -k sk-xxx   # 先看有哪些模型，顺便分类
python -m llm_stress compare `
  --base-url https://your-gateway/v1 --api-key sk-xxx `
  --all-models --limit-models 5 `
  --scene ladder --dataset mixed --start 1 --stop 8 --step 2 --requests-per-stage 20
```

横评是**逐模型串行**跑同一套场景（不是混在一起轮询），每个模型各自出结论、推荐并发与
峰值吞吐，最后按「结论档位 → 推荐并发 → 峰值吞吐 → TTFT p95」排名，产出
`<run_id>.compare.md`（对比表 + 冠军 + 各模型发现/建议 + 复现命令）与
`<run_id>.compare.json`。单个模型失败不会中断整轮，失败原因会写进对比表。

`models` 会把模型名归成对话 / 向量 / 重排 / 图像 / 音频 / 视频 / 审核七类，
并标注厂商家族（Claude / Gemini / OpenAI / DeepSeek / 通义 / Kimi / 豆包 / 智谱 …），
横评默认只挑对话模型，避免拿 embedding 去压 chat。

### Web 界面

```powershell
python -m llm_stress serve            # http://127.0.0.1:8021
```

页面左侧填目标地址、密钥、模型、场景与参数，右侧实时看到进度条、每档指标与最终结论；
底部可以翻历史报告并预览 Markdown。前端是零构建的原生 JS，不需要 node。

点「多模型横评」按钮即走同样的场景参数做多模型对比：模型输入框留空时会自动拉取列表、
只挑对话模型（最多 8 个）；运行中能看到第几个模型、每个模型结束时的结论与推荐并发，
结束后给出对比表与排名。

### 自检（不需要真实 API）

```powershell
python tests\mock_api.py --port 8099 --concurrency-limit 2 --ttft-ms 40 --token-ms 6 --tokens 20
python -m llm_stress run -u http://127.0.0.1:8099 -k test-key -m mock-llm-fast `
  -s ladder --dataset short --start 1 --stop 4 --step 2 --requests-per-stage 6

python -m llm_stress models -u http://127.0.0.1:8099 -k test-key
python -m llm_stress compare -u http://127.0.0.1:8099 -k test-key `
  --model mock-llm-fast,mock-llm-slow -s baseline -n 3 -d short
```

`tests/mock_api.py` 是一个可控的假上游：能设定首字延迟、每 token 间隔、输出长度、
并发上限（超限回 429）和错误率，用来演练压测结论而不烧钱。

---

## 2. 三个场景怎么选

| 场景 | `--scene` | 默认档位 | 回答什么问题 |
| --- | --- | --- | --- |
| 基线 | `baseline` | 并发 1，20 个请求 | 单并发下的最好成绩是多少？后面所有对比都以它为参照。 |
| 阶梯 | `ladder` | 1→2→4→8→16（倍数递增，每档 30 请求） | 加到多少并发开始不划算、开始报错、开始变慢？拐点在哪？ |
| 长稳 | `soak` | 单档并发，默认 600 秒 | 高并发跑久了会不会变慢（漂移）、会不会崩？ |

> 阶梯的档位由 `--start / --stop / --step` 决定，最多 12 档并自动补上终点值。
> 长稳的漂移结论需要至少 20 条成功样本，样本不够会如实标注"漂移结论不成立"。

---

## 3. 指标口径（报告里用的是这五个）

| 指标 | 定义 | 为什么看它 |
| --- | --- | --- |
| **TTFT** | 请求发出到**第一个内容 token** 到达的时间 | 决定"多久开始打字"的体感，通常比总延迟更影响用户评价 |
| **TPOT** | `(总耗时 - TTFT) / (输出 token 数 - 1)` | 吐字是否流畅；TPOT 高说明显存带宽或解码能力吃紧 |
| **输出吞吐** | 阶段窗口内成功请求产生的输出 token 总数 ÷ 窗口秒数 | 容量规划主指标：加并发最终要换来这个数更大 |
| **RPS** | 阶段内成功请求数 ÷ 窗口秒数 | 与吞吐一起看，判断瓶颈在"请求数"还是"生成量" |
| **分位数** | p50 / p90 / p95 / p99，线性插值（与 numpy 默认一致） | 平均值会掩盖长尾，SLO 用 p95 判定 |

几个容易踩的口径细节，本项目已经处理：

- **失败请求不进延迟分布**：429 / 5xx / 超时只算成功率与错误分布，不污染 TTFT 与总延迟分位。
- **窗口定义**：一档的窗口 = 该档最早请求开始 ~ 最晚请求结束，因此 RPS 与吞吐都不是"拍脑袋的均值"。
- **口径统一**：成功率在内部统一成 0~1，报告里按百分比展示，避免 1.0 与 100.0 混用（这个 bug 之前真实存在过，已修）。
- **不经过系统代理**：所有 HTTP 客户端都是 `trust_env=False`。本机开了代理时，压测请求被送进代理会让延迟数据失真，甚至直接报 `InvalidURL`。

---

## 4. 结论是怎么判出来的

判定阈值（`src/llm_stress/decisions.py` 的 `SLO_DEFAULTS`，目前为代码常量）：

| 阈值 | 默认值 | 含义 |
| --- | --- | --- |
| `ttft_p95_ms` | 2000 ms | 首字 p95 超过 2 秒，交互体感明显变差 |
| `total_p95_ms` | 30000 ms | 完整回答 p95 超过 30 秒 |
| `success_rate` | 0.99 | 成功率低于 99% 视为不可用 |
| `drift_ratio` | 1.5 | 长稳后半段 p95 超过前半段 1.5 倍视为漂移 |

规则：

1. **推荐并发** = 满足全部 SLO 的最大档位。
2. **容量拐点** = 相邻档位输出吞吐增幅 < 15% 的位置（只统计成功率 ≥ 95% 的档位，失败档的 token 再多也不算可用容量）。
3. **结论三态**：全部达标且未见拐点 → `可以承载`；达标但已见拐点 → `容量受限`；没有任何一档满足 SLO → `不建议上线`；无数据 → `数据不足`。
4. **最高档不达标时不给"加大并发"的建议**，而是"先按推荐并发上线，再以 1~2 的步长向上复测，不要直接跳到最高档"。
5. **错误归因**（把 429 与真的容量不足区分开）：

| 错误类别 | 结论口径 |
| --- | --- |
| `rate_limited` (429) | 上游限流，不是本机容量问题：客户端加退避或向服务方提升配额 |
| `http_5xx` | 服务端故障或过载保护，降并发重试或换渠道 |
| `timeout` | 排队过深还是单请求本身超时，先降并发复测 |
| `connect` | 连接失败/重置：查 Base URL、代理、TLS、链路 |
| `http_4xx` | 鉴权、模型名、参数问题，与并发无关，先修调用 |
| `stream_broken` | 200 但没有内容 / 流式中断：网关截断或后端不稳 |

6. **成本**：传 `--price-in / --price-out`（每百万 token 美元）后，报告给出总 token、总花费与"每百万 token 有效成本"；不传时如实说明成本为 0 而不是编一个数。

---

## 5. 报告里有什么

`<run_id>.md` 的固定章节：

1. 元信息（运行 ID、目标、掩码后的 key、协议、模型、数据集、超时）
2. **一句话结论** + 关键发现 + 可执行建议
3. **各并发档位指标**：并发 / 请求 / 成功率 / RPS / 输出吞吐 / TTFT p50·p95·p99 / TPOT p50 / 总延迟 p95
4. **容量拐点**：可用档位 N/M（成功率 ≥95%）、峰值吞吐、拐点说明
5. **长稳漂移**（soak）：前后半段 p95 对比
6. **错误分布**、**SLO 违规明细**、**成本**
7. **指标口径**、**复现命令**（可直接复制重跑）
8. **附录**：本次采集的样本条数，说明任何数字都能由 samples 复算

API Key 只以掩码形式进报告（`sk-abc***...`），不会落盘明文。

---

## 6. 目录结构

```
LLM-stress-testing/
├─ llm-stress.cmd              # Windows 一键入口（内置 PYTHONPATH）
├─ pyproject.toml
├─ src/llm_stress/
│  ├─ transport.py             # 三协议流式客户端：SSE 解析、TTFT、token 计量、429/5xx 分类
│  ├─ engine.py                # 单档压测：N 个 worker + 事件队列（假上游限流靠它复现）
│  ├─ metrics.py               # RequestSample / StageStats / 分位数聚合
│  ├─ scenarios.py             # baseline / ladder / soak、容量拐点、长稳漂移
│  ├─ decisions.py             # SLO 判定、推荐并发、错误归因、成本
│  ├─ report.py                # Markdown / JSON / JSONL 三件套
│  ├─ runner.py                # 编排：跑档位 → 聚合 → 判定 → 落盘
│  ├─ datasets.py              # 内置 short/medium/long/mixed，也支持自定义 JSONL
│  ├─ catalog.py               # 模型发现（多路径兜底）+ 分类（对话/向量/重排/图像/…）+ 厂商家族
│  ├─ compare.py               # 多模型横评：逐模型串行跑同一场景、排名、对比报告
│  ├─ cli.py                   # run / compare / models / report / datasets / serve
│  └─ web/                     # FastAPI + SSE + 原生前端（index.html / app.js / style.css）
├─ tests/                      # 89 个用例，含可控假上游的端到端压测与横评
└─ reports/                    # 压测产物
```

---

## 7. 验证状态（可复算）

| 项目 | 结果 |
| --- | --- |
| 单元 + 集成测试 | `89 passed in 12.01s`（`python -m pytest -q`，含真实拉起假上游的端到端用例） |
| CLI 端到端 | 假上游限流 2 并发、ladder 1→4：并发 1 吞吐 53.4 token/s，并发 2 106.4 token/s，并发 4 因 429 掉到 33.3% 成功率；结论"可以承载、推荐并发 2、容量上限落在 2 与 4 之间" |
| Web 端到端 | `/api/meta`、`/api/run`、SSE（progress / stage_done / done）、`/api/reports`、`/api/reports/{name}` 全部返回正常；baseline / ladder / soak / 取消 四种流程均实测通过 |
| 横评端到端 | CLI：`compare` 对 mock-llm-fast/mock-llm-slow 跑 baseline，产物 `.compare.md`（2306 字节）+ `.compare.json`（4554 字节）。Web：`POST /api/compare` 后 SSE 收到 model_start×2 / model_done×2 / done，`done.compare` 带 ranking 与 files；`/api/reports` 出现 `kind=compare` 条目（champion 与模型数正确），详情接口取到 1359 字符 Markdown |

复现测试：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q
```

---

## 8. 已知边界（以及为什么不这么做）

- **不采集 GPU 显存 / 利用率 / KV Cache**：本工具打的是别人的网关或官方 API，没有服务端权限。要这些指标请在被压服务上用 `nvidia-smi` + Prometheus，本工具负责给出"从调用方看得到的"延迟与吞吐。
- **不集成 `vLLM benchmark_serving`、EvalScope**：它们面向自建推理服务端与离线评测，观测点比黑盒更靠内；作为调用方压测，用不上它们的进程内指标。
- **不用 Locust / k6 / JMeter**：跨语言 + 跨协议的代价太大——三套 SSE 封包、usage 计量、429 退避、`stream_options` 兼容重投都要重写一遍，而这些正是 `transport.py` 已有的能力。
- **单机压测的上限**：当连接错误占比明显上升时，瓶颈可能已经在客户端（端口、网卡、Python GIL），此时结论只代表"这台机器打出的上限"。
- **SLO 阈值暂未开放 CLI 覆盖**：以 `decisions.py` 的 `SLO_DEFAULTS` 为准；改阈值改代码，报告会原样反映实际生效的阈值。

---

## 9. 从"指标"到"决策"的对应关系

| 方法论要点 | 本项目的落点 |
| --- | --- |
| TTFT 决定体感 | `transport.py` 在首个内容 token 处打点，报告给 p50/p95/p99 |
| 生成速度看 TPOT | `metrics.RequestSample.tpot_ms` + 档位表 TPOT p50 |
| 总吞吐 | `StageStats.output_tps`（成功请求的 token/窗口），是推荐并发的主依据 |
| 并发要阶梯加压找拐点 | `scenarios.ladder` + `find_saturation`（增幅 <15% 判饱和） |
| 长稳测漂移 / 泄漏 | `scenarios.soak` + `drift_of`（后段/前段 p95 ≥1.5 判漂移） |
| 真实长短混合数据集 | `datasets` 内置 short/medium/long/mixed，可加载业务 JSONL |
| P50/P90/P95/P99 + 5xx | `metrics.aggregate` 输出全分位与状态码/错误分布 |
| 拐点与崩溃点分离 | 结论区分"容量受限"（拐点）与"不建议上线"（连一档都不达标），并把 429 单独归因 |
| 复现 | 报告内置复现命令、run_id 与逐请求 samples |
| 多模型同杆比较 | `compare` 逐模型串行跑同一档位序列，排名先看结论再看推荐并发/吞吐/TTFT，并标出失败模型原因 |
