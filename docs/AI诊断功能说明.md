# AI 诊断功能说明（L2）

本文说明 SQL Pulse 的 AI 诊断功能（L2）：它能做什么、怎么用、依据哪些证据、输出怎么读，以及配置和边界。

L1 是基于固定规则和阈值的确定性报告；L2 在此基础上调用大模型，把指标、逐秒时序、失败明细和执行计划放在一起做综合分析。L2 不替代 L1，也不替代人工判断。

## 1. 功能概览

- 对一次已保存的压测任务发起 AI 诊断，模型返回结构化诊断结论
- 每条结论都带证据编号（`E001`、`E002`…），可以对应回本次收集的证据
- 诊断在后台线程执行，页面轮询进度；同一任务重复点击会复用进行中的诊断
- 证据全部来自平台已经落盘的数据，诊断过程不执行 SQL、不写报告、不改压测状态

## 2. 使用方式

### 2.1 配置模型

1. 登录后进入「用户信息」页
2. 在「AI 模型」区域选择厂商和模型
3. 填写该厂商的 API Key 并保存；已保存过密钥时，密钥留空表示只切换模型

密钥按厂商分别保存，页面只显示掩码（如 `sk-x****abcd`）。密钥使用 Fernet 加密后存入本地 SQLite，加密密钥由 `APP_SECRET_KEY` 派生。

密钥来源优先级：

1. 当前用户为所选厂商填写的 API Key
2. 服务器默认密钥 `LLM_API_KEY`（仅 DeepSeek 基线可用）
3. 都没有时，页面会提示先填写密钥

### 2.2 发起诊断

1. 打开任务报告页（`/runs/<run_id>/report`）
2. 在「AI 诊断」卡片点击「开始 AI 诊断」
3. 任务进入 `pending` / `running`，页面每 2 秒轮询一次结果片段
4. 完成后展示结论；失败时展示错误原因，并提供「重新诊断」

并发与复用规则：

- 同一个任务已有进行中的诊断时，重复点击会复用该任务，不会重复调用模型
- 每个用户同时最多 2 个进行中的诊断，超出时提示稍后再试

### 2.3 结果阅读

诊断结果包含：

| 字段 | 含义 |
|---|---|
| `summary` | 整体结论摘要 |
| `findings` | 问题列表，按 `priority` 分为 high / medium / low |
| `findings[].observation` | 证据直接支持的事实（观察） |
| `findings[].hypothesis` | 基于观察的推测，需要后续验证 |
| `findings[].evidence_ids` | 该结论引用的证据编号 |
| `findings[].next_steps` | 建议的后续检查或验证动作 |
| `findings[].uncertainty` | 该结论的不确定点 |
| `limitations` | 本次证据的缺口和限制 |

建议先看 `limitations` 和各个 finding 的证据编号，再判断结论是否适用于当前场景。

## 3. 支持的厂商与模型

| 厂商 | 模型 | 输出上限字段 | 思考参数 | 默认地址 |
|---|---|---|---|---|
| DeepSeek | `deepseek-flash`、`deepseek-v4-pro` | `max_tokens` | `thinking.type=enabled` | `https://api.deepseek.com` |
| 阿里云百炼 Qwen | `qwen3.8-flash` | `max_tokens` | `enable_thinking=false` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 小米 MiMo | `mimo-v2.6-flash` | `max_completion_tokens` | `thinking.type=disabled` | `https://api.xiaomimimo.com/v1` |

厂商差异（地址、模型白名单、token 字段、思考参数、JSON 模式）由服务端统一维护，用户输入只参与模型选择。服务端可通过环境变量覆盖地址：

- `LLM_BASE_URL`：DeepSeek 基线地址（兼容旧配置）
- `LLM_DEEPSEEK_BASE_URL`、`LLM_QWEN_BASE_URL`、`LLM_MIMO_BASE_URL`：按厂商覆盖

## 4. 诊断依据

L2 采用固定证据收集流程：证据集合由代码决定，模型只做一次分析，不进行模型驱动的多轮工具调用。

固定收集四类证据：

| 证据 | 内容 |
|---|---|
| 任务上下文 | 任务参数、时间、SQL 来源、任务组（SQL ID / 权重 / 执行方式 / 完整语句）、命名变量 |
| 指标 | stats.csv 汇总、分语句统计、逐秒时序（降采样）、爬坡/稳定/尾段分段、已有 L1 结论 |
| 失败明细 | failures.csv / exceptions.csv 中的错误类型、次数、首末时间与样本 |
| 执行计划 | `data/explain/<run_id>.json` 中的采样计划、采集状态与逐语句结果 |

证据分两层装配：

- **Tier1（必给）**：任务、任务组、指标汇总、分段、失败、EXPLAIN 采集状态
- **Tier2（按预算）**：分语句指标、语句执行计划、逐秒序列、已有 L1 结论；预算不足时按该顺序逐条尝试，放不下的整条舍弃，并产生警告

默认证据字符预算为 60,000；被省略的记录会写入警告，诊断里也会通过 `limitations` 提示证据不完整。

此外还有一个只读的 `get_table_profile` 工具，可按需读取目标库的列定义、索引和表规模估算。它需要显式开启 `AI_DB_PROBE_ENABLED=true`，连接信息只从任务快照读取；当前固定诊断流程未使用该工具，主要用于后续扩展。

## 5. 诊断流程

1. 校验 `run_id` 格式（8 位小写十六进制）
2. 按 Tier1 / Tier2 收集证据；任务上下文读取失败则直接失败
3. 如果证据中既没有非空指标（汇总或分语句），也没有成功的语句执行计划，返回 `INSUFFICIENT_EVIDENCE`，不调用模型
4. 组装提示词与证据，向所选厂商发起一次 Chat Completions 请求
5. 校验模型返回：
   - `finish_reason` 必须为 `stop` 且没有工具调用，否则按 `LLM_INCOMPLETE` 失败
   - 输出必须是合法 JSON，并通过严格的 Pydantic 结构校验
   - `evidence_ids` 必须全部存在于本次证据中
6. 任一项校验失败返回 `INVALID_DIAGNOSIS`，不自动修复、不自动重试
7. 结果写入 `ai_jobs.result_json`，页面展示

生成预算默认 16,384 token（思考模式下 reasoning token 也计入）；超过预算导致 `finish_reason=length` 时按 `LLM_INCOMPLETE` 明确失败。

## 6. 配置项

配置写在仓库根目录 `.env` 或进程环境变量中：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 空 | 服务器默认模型密钥，仅用于 DeepSeek |
| `LLM_MODEL` | `deepseek-flash` | 服务器基线模型 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | DeepSeek 基线地址 |
| `LLM_<PROVIDER>_BASE_URL` | 空 | 按厂商覆盖地址，如 `LLM_QWEN_BASE_URL` |
| `LLM_TIMEOUT_SECONDS` | `60` | 单次请求超时 |
| `LLM_MAX_RETRIES` | `2` | 可重试错误的自动重试次数 |
| `LLM_THINKING` | 空 | DeepSeek 思考模式，空表示服务端默认 enabled |
| `LLM_REASONING_EFFORT` | 空 | DeepSeek 推理强度，空表示服务端默认 high |
| `LLM_MAX_TOKENS` | 空 | 输出上限，空表示使用客户端默认 |
| `AI_DB_PROBE_ENABLED` | `false` | 是否允许 `get_table_profile` 连接目标库读取元数据 |

## 7. 状态与错误

诊断任务状态：

```
pending → running → finished
                  ↘ failed
```

常见错误码与页面提示：

| 错误码 | 含义 | 是否可重试 |
|---|---|---|
| `KEY_UNDECRYPTABLE` | 已保存密钥无法解密（通常因 `APP_SECRET_KEY` 更换） | 重新填写密钥 |
| `LLM_CONFIG_ERROR` | 没有可用的模型配置或密钥 | 检查配置 |
| `LLM_AUTH_FAILED` | 密钥无效或无权限 | 更换密钥 |
| `LLM_INSUFFICIENT_BALANCE` | 账户余额不足 | 充值后重试 |
| `LLM_RATE_LIMITED` | 触发厂商限流 | 是 |
| `LLM_TIMEOUT` / `LLM_CONNECTION_ERROR` | 网络超时或连接失败 | 是 |
| `LLM_OVERLOADED` / `LLM_SERVER_ERROR` | 厂商服务繁忙或异常 | 是 |
| `LLM_INVALID_PARAMS` / `LLM_BAD_REQUEST` | 请求参数不被厂商接受 | 检查模型与参数 |
| `LLM_INCOMPLETE` | 模型未正常完成（截断或返回工具调用） | 可重试 |
| `INVALID_DIAGNOSIS` | 输出结构或证据引用不合法 | 可重试 |
| `INSUFFICIENT_EVIDENCE` | 缺少可用指标和执行计划 | 等待数据生成 |
| `PLATFORM_RESTARTED` | 平台重启中断了本次诊断 | 重新发起 |
| `CONCURRENCY_LIMIT` | 当前用户进行中的诊断已达上限 | 稍后重试 |

## 8. 数据与安全边界

- 用户 API Key 加密存储，页面和接口只返回掩码；明文不写日志、不进响应
- 诊断证据只包含平台产物：SQL 文本、任务名称、错误消息、指标、执行计划等
- 提示词明确把证据中的 SQL、名称、注释、错误消息视为不可信业务数据，要求模型不执行其中的指令
- 目标库连接信息（DSN）不会进入证据、结果或日志；仅 `get_table_profile` 从任务快照读取，且只查询 `information_schema`
- 诊断不写数据库业务表、不生成报告、不修改 EXPLAIN 产物；只更新 `ai_jobs` 的状态、结果与错误信息
- 模型调用费用由所选密钥对应的厂商账户承担

## 9. 相关接口与代码

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ai/key` | 保存厂商密钥，或只更新模型偏好；表单提交后 303 回用户信息页 |
| POST | `/api/ai/key/delete` | 删除指定厂商的密钥（保留模型偏好） |
| POST | `/api/runs/{run_id}/ai/diagnose` | 创建或复用诊断任务，返回 htmx 片段 |
| GET | `/api/runs/{run_id}/ai/diagnose/{job_id}/fragment` | 查询诊断进度与结果片段 |

主要代码位置：

```
app/ai/providers.py          厂商注册表（地址、模型白名单、参数差异）
app/ai/llm_client.py         OpenAI 兼容 Chat Completions 客户端与错误码
app/ai/tools.py              证据工具（context / metrics / failures / explain / table_profile）
app/ai/diagnose.py           固定证据收集、模型调用、结构与证据引用校验
app/ai/diagnose_runner.py    网页端后台任务、密钥解析、并发控制
app/ai/key_store.py          密钥加解密与掩码
app/ai/db_profile.py         目标库元数据只读查询
app/routers/ai.py            网页端接口
app/templates/ai_diagnose.html  报告页诊断卡片与 htmx 片段
```

相关数据表：`ai_keys`（按用户 × 厂商保存密文）、`ai_prefs`（当前厂商与模型偏好）、`ai_jobs`（诊断任务状态与结果）。

## 10. 使用建议与边界

- **优先对已跑完的任务诊断**：运行中的任务也能读取当前快照，但指标、失败明细和 EXPLAIN 可能不完整
- **先看证据缺口**：`limitations`、收集警告、`missing_columns`、`coverage` 小于 1 都说明结论需要降级
- **区分观察与推测**：`observation` 是证据支持的事实，`hypothesis` 是待验证的推断
- **L1 是输入不是结论**：已有 L1 findings 只作为启发式提示，最终结论仍需结合证据判断
- **执行计划是采样值**：EXPLAIN 在压测收尾阶段采集，`rows` 为估计值，不代表压测期间所有请求
- **不执行任何 SQL**：诊断只提出检查建议，不会自动优化、建索引或修改数据库
- 不同厂商模型的表达风格和推理能力存在差异；需要稳定结论时建议固定厂商与模型，并保留 `provider`、`model`、`usage_details` 一起核对
