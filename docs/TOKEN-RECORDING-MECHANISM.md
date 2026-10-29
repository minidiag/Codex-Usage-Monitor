# 压缩日志、Codex / Work 会话与 Token 记录机制

核查时间：2026-10-29，北京时间约 13:44–13:49。依据：当前项目源码、本机只读 SQLite 查询及日志中的标识和数值字段。核查期间采集器继续运行，各次读取不是同一个原子快照；正在运行的本次会话不用于跨阶段差额判断。没有修改 Codex 日志、生产账本、凭据或上传数据。

## 1. 结论

现有监控账本统计的是 **`event_msg/token_count.info.total_token_usage` 从采集基线开始的增量**。它不是全部模型请求的完整账单。

- `.jsonl.zst` 是日志文件的无损压缩副本，本身不产生模型 Token；现有代码只核实其中已采集的历史记录，不自动导入额外用量。
- `compacted` 是模型上下文压缩事件。实测其调用用量出现在顶层 `token_usage_record` 中，并在 `compacted.payload.latest_token_usage_record` 保存一份相同记录。
- 当前解析器忽略 `token_usage_record`，看到 `compacted` 只标记覆盖不足。因此，普通累计检查点没有包含的压缩调用用量，不会进入当前账本。
- Codex Desktop 和本机 Work 会话共用同一种 rollout / SQLite 记录结构。能否采集主要取决于日志来源、基线、文件续接及排除状态，不能只根据界面上的 Codex / Work 分类判断。
- 项目归属不是当前 Token 上传协议的统计维度；会话 Token、项目合计和账号额度是不同的数据口径。

## 2. 实际日志规模与字段

本机 `C:\Users\<USERNAME>\.codex` 下，当次清点有 51 个普通 JSONL、159 个 Zstandard 压缩日志。索引共有 192 个线程，其中 32 个常规主会话：25 个 Codex Desktop、5 个 Work、2 个 exec；其余是 guardian / subagent 线程，本次不将它们的用量合并进主会话。

对 32 个主会话，读取了它们目前索引指向的日志，并按日志内 `session_meta.id` 找出此前的文件段，共核查 50 个来源文件，其中 28 个是压缩文件，解码读取约 306.6 MB。该范围发现 6,365 条顶层 `token_usage_record`、53 条 `compacted`；其中 52 条压缩事件带 `latest_token_usage_record`。

| 记录位置 | 含义 | 当前采集器如何处理 |
|---|---|---|
| `session_meta.payload.id` | 日志所属的逻辑会话标识 | 转换为匿名 `stream_id` |
| `turn_context.payload.model` | 模型元数据 | 用于后续检查点的模型标签 |
| `event_msg/token_count.info.total_token_usage` | 普通累计用量检查点 | 读取，保存累计值，服务端做差 |
| `event_msg/token_count.info.last_token_usage` | 最近请求或上下文状态相关显示数据 | 不使用 |
| 顶层 `token_usage_record.payload.usage` | 单次响应的用量 | 忽略 |
| `token_usage_record.payload.turn_token_usage` | 当前轮次累计用量 | 忽略 |
| `token_usage_record.payload.thread_token_usage` | 此记录体系下的线程累计用量 | 忽略 |
| `compacted.payload.latest_token_usage_record` | 压缩时保存的最新用量记录；在本次 52 个样本中对应压缩响应 | 只标记 `unsupported_usage`，不取数 |
| `state_5.sqlite → threads.tokens_used` | 会话列表中的旧累计值 | 活跃会话读取器会读取；启用身份过滤时，展示值由本地账本重新计算 |

31 个当前日志有普通用量检查点；初次对照时，这 31 个会话的末尾 `total_token_usage.total_tokens` 都等于索引 `threads.tokens_used`。另一个 exec 会话无用量检查点、索引值为 0。**索引一致证明的是这两条旧口径一致，不能证明已覆盖压缩请求。**

`total_tokens = input_tokens + output_tokens`；缓存输入和推理输出是子集，不能再分别加一次。另有 `cache_write_input_tokens` 出现在新记录字段中，当前四计数器协议没有保留它。

代码依据：[source.py](../client/source.py)、[active_sessions.py](../client/active_sessions.py)、[agent.py](../client/agent.py)。

## 3. 文件压缩与上下文压缩如何影响计数

### 文件 `.jsonl → .jsonl.zst`

当前普通扫描器处理 JSONL。原文件消失但同名 `.zst` 存在时，`verify_archive()` 在内存中解压，核对会话标识、原基线、所有已保存检查点、已读取位置与文件末尾，并缓存核实结果。

核实过程不创建新流、不新建捕获基线、不追加 Token 事件。待上传事件仍沿用原 ID 重试；原流没有暂停、核实通过且已有事件全部确认后，才显示 `synced`。因此“历史来源已压缩并同步”表示已采集范围有证据支持，不代表完整历史已导入。

未登记压缩历史、压缩前漏读的新增记录、解压损坏或超限等情况会留下覆盖缺口。压缩核实上限为 128 MiB。代码依据：[source_archives.py](../client/source_archives.py)。

### 模型上下文 `compacted`

52 个带用量的压缩事件全部满足：

1. `compaction_response_id == latest_token_usage_record.response_id`。
2. 文件中存在同一 `response_id` 的顶层 `token_usage_record`。
3. 两处的 `usage`、`turn_token_usage`、`thread_token_usage` 数值完全一致。

所以，**同一压缩响应只能记一次**。不能把顶层 `usage` 和 `compacted.latest_token_usage_record.usage` 相加，也不能把单次、轮次、线程三个层级互相相加。

51 个有完整前后普通检查点的带用量压缩样本，压缩前后的 `total_token_usage` 完全不变。另一个样本缺少压缩前普通检查点，不纳入该比较。压缩后 `last_token_usage` 还会出现四个分项全为 0、而 `total_tokens` 为正的情况；例如 17,468。该值不能直接作为新增消耗相加，字段行为与单次请求用量不一致。

连续且可对照的实际会话示例（标识只列前缀）：

| 会话 | 旧 `token_count` / 索引累计 | 新 `thread_token_usage` 累计 | 差额 | 已识别压缩响应 `usage` 合计 |
|---|---:|---:|---:|---:|
| `01a0ad3d…` | 38,382,623 | 39,308,438 | 925,815 | 925,815 |
| `01a116e4…` | 94,467,249 | 96,488,182 | 2,020,933 | 2,020,933 |
| `01a17703…` | 41,577,373 | 41,871,638 | 294,265 | 294,265 |

这些等式直接支持“普通累计检查点漏掉压缩调用”的判断。本次 52 个不同压缩响应的 `usage.total_tokens` 合计为 **12,239,215**。这是所核查历史样本的压缩调用量，不能直接当作当前监控周期应补传的量：其中可能发生在采集基线之前，且本次没有完成账号、历史段和周期的补账验证。

也不能直接把解析字段从旧累计值替换成 `thread_token_usage`：部分旧会话存在记录格式起始时间差异或累计重置。实测一个会话发生普通累计值回退 11,218,564；另一个旧会话的新记录体系累计低于旧累计。原因尚未逐一验证，应划分计数区段并验证边界，不能强行做差或统一加常数。

官方 OpenAI documentation 确认上下文压缩用于降低后续上下文大小，独立 compact 响应含压缩过程的 `usage`。上述本机日志路径、重复关系及计数差异是本次本地观测，不是由官方 API 文档推断出来的桌面产品保证。参考：[Compaction](https://developers.openai.com/api/docs/guides/compaction)、[Compact conversation](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)。

## 4. Codex、Work 和项目会话怎样关联

5 个 Work 主会话的 `originator` 为 `codex_work_desktop`，但 `session_meta.source` 都是 `vscode`；其中 3 个 `thread_source=chatgpt_handoff`，2 个为 `user`。本项目允许 `vscode`，并不因 `chatgpt_handoff` 字样而拒绝该普通主会话，因此这些本机 Work 日志能够走现有采集路径。

整个样本中，6,365 条新用量记录的 `thread_id` 和 `session_id` 都等于所属日志的 `session_meta.id`。`compacted` 另有 `window_id` / `previous_window_id`，它们在核对样本中不同于用量记录中的 `session_id`。不能把窗口 ID、文件名的第二个 UUID 和逻辑线程 ID 当成同一概念。

8 个逻辑主会话具有多个日志文件段；其中一个 Work 会话 `01a1572a…` 同时具有原始 `.jsonl.zst` 和后续带第二个 UUID 的 JSONL。两个文件内的 `session_meta.id` 相同，所以不能按文件数量新增两个会话并相加累计值，也不能只跟随索引的最新路径而丢掉所有旧段。

该 Work 样本的生产账本显示：原段末尾 12,522,595 被保存为捕获基线，后续段已在 `source_exclusions` 中排除，索引末尾为 33,271,045，本地流只有基线、没有增量事件。这里的差异来自基线和已保存的来源排除范围，不是上传队列积压；不能直接将两者差额补到今天。

相对地，2026-10-29 的 Work 会话 `01a185b4…` 保存基线 31,961，末尾旧累计 2,291,012，已有 29 条后续检查点，流状态 ready、待传 0。该账本采集范围的普通增量为 **2,259,051**。它小于会话完整累计，是首次请求已成为捕获基线的预期结果。

这只覆盖本机有 rollout 的 Work 会话；没有验证远端 ChatGPT 支撑的全部 Work 对话是否具有本机日志，不能据此宣称整个 Work 账户完整覆盖。

项目维度方面：本次 32 个主会话在 `threads.project_id` 中全为 NULL；采集器没有读取该字段，上传协议也没有 `project_id` 或等价的项目归属字段。现有统计维度是流、设备、用户、账号及周期。仅凭当前遥测数据无法还原 Codex / Work 项目合计，也不能把 `cwd` 自动当作项目身份。

## 5. 当前账本怎样落地、去重和汇总

数据流为：

```text
sessions / archived_sessions 中的 JSONL
  → 读取 session_meta、turn_context、token_count 累计值
  → HMAC(session UUID) 得到匿名 stream_id
  → 首次扫描的最后检查点设为 capture_start 基线
  → SQLite 同一事务保存来源游标、流进度、不可变事件
  → 持久 outbox 上传累计快照
  → 服务端按连续序号验证，累计快照做差
  → usage_deltas 按事件时间汇总为窗口 / 额度周期统计
```

具体规则：

- 新来源即使已完成多个请求，第一次读到的最后累计值仍只当基线。这会漏掉首次发现之前的起始段，但避免把历史总量归到启用当天。
- 本地事件 ID 由流标识、epoch、序号确定；复制、重试等须验证相同锚点和不可变内容，服务端也检查序号和语义哈希。
- 当前客户端固定 `epoch=0`，并以文件内有效 `token_count` 的次序计算序号；它没有使用新格式的 `response_id` 去重，也没有完整的跨文件区段续接适配。无法复现原锚点时会暂停，或按已保存来源核实结果排除。
- 服务端只对相邻连续快照做差，基线不产生历史增量。计数回退或子集关系异常会导致暂停 / 隔离。增量归入后一个检查点的事件时间，不能把长期缺口精确分摊回每次请求。
- 活跃列表基础读取器读取 `threads.tokens_used`；启用用户身份范围后，`Agent.active_view()` 用本地事件的 input + output 差额重新计算，仅显示匹配当前用户和账号范围的段。该展示值不额外加入服务器账本。
- 额度百分比来自单独的官方额度接口，不能由上述 Token 合计换算。

代码入口：`client/source.py:50`、`client/core.py:120`、`client/core.py:172`、`client/agent.py:174`、`server/store.py:61`、`server/store.py:131`、`server/protocol.py:93`。

## 6. 后续适配应遵守的边界

优先实现独立的新格式适配器，以 `thread_id + response_id` 识别单次用量，并验证跨文件、重放及继承行为；`compacted.latest_token_usage_record` 应作为同一响应的校验证据或缺失补充入口，不能成为第二份消耗。旧检查点适合作为兼容来源与对账依据，不能与新单次响应流叠加。

导入压缩历史需要独立的只读解码、会话归并、格式版本与计数区段识别、原事件时间归属及试算差额报告。须区分基线前历史与启用后的缺口，验证后才能迁移账本；不能在现有流中直接换累计口径。

若需要项目合计，应建立明确的会话到项目映射，并定义会话移动项目后是重算历史还是仅影响后续记录，再扩展协议和汇总。现有账号或工作目录字段不能替代该映射。

## 7. 验证及本地复查材料

运行 `python -m unittest client.tests.test_collector client.tests.test_source_archives client.tests.test_active_sessions`，43 项现有测试全部通过。这些测试确认当前基线、归档核实与活跃列表行为；它们没有证明尚未实现的新格式适配正确。

本次只读核查脚本和数值证据保存在已忽略的 `.local/token_mechanism_audit.py`、`.local/token_mechanism_audit.json`。JSON 仅保存标识、结构及数值，不保存对话正文。执行脚本只读取本机日志和生产数据库，输出写入项目 `.local`；重复执行时正在运行会话的数值会继续变化。
