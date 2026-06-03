# Workflow Timeline 重新设计提案

> **版本**: 0.8（草案）
> **日期**: 2026-06-02
> **目标**: 把"工作流时间线"从粗粒度节点流水账，重构为只展示**有意义的、动态的、按任务类型差异化的若干个主要步骤**。
> **详细子步骤**: 继续保留在 `Task Logs` 中。
> **适用范围**: 办公任务（analyze / summarize / organize）+ 开发任务（Jira → PR）。
>
> **语言约定**：
> - 文档解释、讨论、需求分析使用中文。
> - **最终实现一律使用英文**：所有 step_key、title、agent、summary_template、summary_facts 的字段名与值都是英文。
> - 前端 UI 文案（即用户能看到的字符串）也使用英文，与 `agents/compass/ui/templates.py` 现状保持一致。

---

## 0. v0.8 决议：统一契约与边界

本节是后续实现的最高优先级契约。若本文后续示例与本节冲突，以本节为准。

### 0.1 单一公共 API

所有 Agent 只能通过 `framework.major_step.record_major_step` 记录时间线步骤。Agent 代码不得直接写入 `task.metadata.major_steps` / `major_step_rows` / `step_states` / `step_summaries` 等字段。

公共 API 的目标不是让每个 Agent 自己发明 UI 字段，而是把所有 Agent 的 major step 统一成同一种事件：

```json
{
  "task_id": "task-abc",
  "orchestrator_task_id": "task-abc",
  "step_key": "office.writing",
  "step_instance_key": "office.writing#0",
  "round": 0,
  "title": "Office writing deliverable",
  "agent": "office",
  "lifecycle_state": "running",
  "visual_state": "current",
  "summary_template": "Office wrote {output_count} analysis report(s) to {output_location}.",
  "summary_facts": {
    "output_count": 1,
    "output_location": "the workspace"
  }
}
```

### 0.2 Compass task is the UI source of truth

Compass UI 只读取 Compass 顶层任务的 `metadata`。因此任何下游 Agent（Office / Team Lead / Web Dev / Code Review）产生的 major step，最终都必须合并回 Compass 顶层任务。

实现上允许两种路径，但语义一致：

| 场景 | 写入路径 | 要求 |
|---|---|---|
| Agent 与 Compass 共用同一个 `TaskStore` | `record_major_step(..., task_store=shared_store)` 直接更新 Compass task | 必须使用 Compass task id / `orchestrator_task_id` |
| Agent 拥有独立 `TaskStore`（Office per-task、容器、远程 Agent） | Agent 本地记录步骤，同时通过已授权的进度通道把 `major_step_event` 发送给 Compass | 进度通道必须来自 Capability Registry 或 A2A message metadata 中已授权 callback/capability；不得硬编码 Agent URL |

Compass 收到下游 `major_step_event` 后，用同一个合并函数更新 Compass task metadata。UI 不读取 Office 自己的 task metadata，也不依赖跨进程共享内存。

### 0.3 行模型：event 与 row 分离

本文统一使用两个概念：

| 名称 | 字段 | 语义 |
|---|---|---|
| Major step event | `major_step_events` | 追加式事件流。每次调用 `record_major_step` 都追加一条事件，便于诊断和回放 |
| Major step row | `major_step_rows` | UI 渲染用的当前行集合。同一个 `(step_key, round)` 更新同一行 |

`major_steps` 在迁移期保留为兼容字段，但新实现应读写 `major_step_events` 和 `major_step_rows`。若为了兼容继续输出 `majorSteps`，它应映射自 `major_step_rows`，不是原始事件流。

### 0.4 唯一步骤实例键

任何索引字段都必须使用 `step_instance_key`，而不是只用 `step_key`。

规则：

```text
step_instance_key = f"{step_key}#{round}"
```

示例：

- `wd.rebuilding#1`
- `wd.rebuilding#2`
- `tl.requesting_user_input#1`
- `tl.requesting_user_input#2`

这解决循环、多次用户输入、同一步多轮 retry 覆盖彼此的问题。

### 0.5 生命周期状态与视觉状态分离

`lifecycle_state` 表示真实执行状态；`visual_state` 只表示 UI 呈现。

| lifecycle_state | visual_state | 含义 |
|---|---|---|
| `pending` | `pending` | 尚未到达 |
| `conditional_pending` | `conditional_pending` | 条件步骤，本任务可能不会触发 |
| `running` | `current` | 正在执行 |
| `waiting_for_user` | `warn` | 已暂停，等待用户输入 |
| `resuming` | `current` | 用户已回复，原任务正在恢复 |
| `done` | `done` | 成功完成 |
| `warning` | `warn` | 可降级警告，但任务继续 |
| `failed` | `failed` | 本步骤不可恢复失败 |
| `cancelled` | `failed` | 用户取消 |
| `terminated` | `failed` | 操作员或 watchdog 终止 |

`warn` 不得自动写 `ended_at`。只有 `done` / `failed` / `cancelled` / `terminated` 写 `ended_at`。等待用户时，右侧耗时继续基于 `started_at -> now` 实时增长；用户回复后，等待行转为 `done` 并冻结 `ended_at`。

### 0.6 指针字段

不要用一个 `current_major_step_key` 同时表达运行中步骤、失败步骤和终态步骤。Compass task metadata 需要保留以下指针：

| 字段 | 用途 |
|---|---|
| `active_step_instance_key` | 当前正在运行或等待用户的步骤 |
| `last_step_instance_key` | 最近一次被写入或更新的步骤 |
| `failed_step_instance_key` | 第一个或最关键的失败步骤 |
| `terminal_step_instance_key` | 任务最终终态行，如 `compass.task_failed#0` |

默认折叠视图优先展示：

1. `active_step_instance_key`（任务运行中或等待用户时）
2. `failed_step_instance_key`（任务失败且有失败步骤时）
3. `terminal_step_instance_key`（任务已取消、终止、失败但没有更具体失败步骤，或完成时需要终态行）
4. `last_step_instance_key`

### 0.7 终态保护

一旦写入 `cancelled` / `terminated` / `failed` 终态行，`record_major_step` 不允许再追加普通步骤。后续迟到的下游事件应被记录到 `major_step_events`，但在 `major_step_rows` 中标记为 `ignored_after_terminal=true`，UI 默认不展示，只在诊断模式或 Task Logs 中可查。

### 0.8 output mode 和任务类型扩展原则

- Office `workspace` / `inplace` 两种模式必须共用同一套 capability skeleton，差异只体现在 `summary_facts.output_location`、验证规则和权限结果。
- 新 Office capability 或新开发 Agent 必须只新增 Agent 自己调用的 `step_key` / `summary_template`，不得修改 Compass UI 渲染逻辑。
- 共享逻辑放在 `framework.major_step`；任务类型特定的 step 定义可以放在 Agent 本地的小型 helper，但输出事件必须符合本节统一契约。

---

## 1. 设计目标与原则

### 1.1 现状问题

| 现状 | 问题 |
|---|---|
| 开发任务固定 7 个固定标题（Plan / Implement / Build / Test / Self-check / Fix / Review & Deliver） | 标题抽象、占用空间大；用户看不出"谁在做、做什么" |
| 办公任务只展示 Compass 写入的粗粒度步骤（"Office task completed"），看不到 Office 节点真正做了什么 | 用户完全感知不到 `receive_task` / `analyze_request` / `execute_office_work` / `report_result` 这些关键节点 |
| 所有办公任务共用同一份通用 timeline | analyze / summarize / organize 的内部动作差异很大（analyze 算统计、summarize 写摘要、organize 搬文件），但时间线看不出来 |
| 所有阶段共用 `deriveMajorPhases` 通过关键词文本匹配归桶 | 阶段名强耦合实现日志措辞；同义不同写法的日志无法稳定归类 |
| `currentMajorStep` 是按时间追加的"最新一段话"，经常包含运行细节 | 摘要与阶段语义不一致 |

### 1.2 目标

让"工作流时间线"成为**任务进度的步骤清单**，每一步都告诉用户"谁在做什么"：

- **主要步骤（major step）= 一次明确的执行单元**：每步以"执行者 + 动作 + 对象"格式命名，可读且不抽象。
- **不同任务类型使用不同的步骤集**：开发任务和办公任务步骤集不同；办公任务内部按 capability 拆分，analyze / summarize / organize 各有自己的骨架。
- **动态摘要必须与步骤语义对齐**：根据当前任务实际产生的数据写一句话，而非日志原文。
- **不硬编码任何用户数据**：摘要模板只描述语义骨架，运行时由 Agent 注入实际数据。

### 1.3 设计原则

| 原则 | 说明 |
|---|---|
| **执行者可见** | 每步标题必须包含执行者 Agent（compass / team-lead / web-dev / code-review / office） |
| **动作明确** | 用动词或动名词描述"在做什么"，而不是抽象名词 |
| **任务类型驱动骨架** | 不同任务类型有不同骨架，骨架条目数量 8–20 之间 |
| **按 capability 拆分办公骨架** | analyze / summarize / organize 各自独立骨架，避免无关步骤干扰用户 |
| **条件步骤独立标记** | 仅有部分任务会经历的步骤（如 Review 退回、Self-check 失败重试、用户未指定 output_mode）单独标记 `(conditional)` |
| **失败有专门位置** | 失败任务在时间线里通过 `failedStepInstanceKey` 反映"哪一步卡住"，而不是把整条标红 |
| **与 Task Logs 互补** | 时间线 = 步骤进度；Task Logs = 全量诊断。两者不能互相替代 |
| **所有实现英文** | step_key / title / summary_template / summary_facts 全部英文；用户可见 UI 文案也英文 |

---

## 2. 主要步骤的通用结构

### 2.1 卡片视觉（沿用现状）

```text
┌─────────────────────────────────────────────────────────┐
│ ●  Step title (actor + action + object)                 │
│                                  06-02 09:00:00  1m 02s │
│ Agent: dynamic summary (template + facts)                │
└─────────────────────────────────────────────────────────┘
```

Display format on the right-hand side is **two inline values**:

- `MM-DD HH:MM:SS` — start time of the step (local timezone, derived from UTC).
- `Xh Ym Zs` — time spent; `h` is hours, `m` is minutes, `s` is seconds. Examples: `0m 02s`, `1m 02s`, `1h 23m 04s`. Zero hours and zero minutes are still emitted (so `0m 02s` not `2s`) to keep columns aligned.

Steps that have not yet fired show `Not started yet` in the same position.

### 2.2 摘要模板

每种任务类型都有自己的摘要模板。模板里只写骨架 + 占位符，**占位符对应的真实数据由 Agent 在调用 `record_major_step` 时一并写入事件**。

| Placeholder | Meaning |
|---|---|
| `{source_count}` | number of input files / folders |
| `{source_kind}` | input type (files / folder) |
| `{capability}` | office capability (summarize / analyze / organize) |
| `{output_count}` | number of generated deliverables |
| `{jira_key}` | Jira ticket key |
| `{branch_name}` | local git branch |
| `{files_changed}` | number of changed files |
| `{test_pass}` / `{test_total}` | passed / total tests |
| `{pr_number}` / `{pr_url}` | PR number / URL |
| `{review_verdict}` | review verdict |
| `{round}` | loop iteration number |

### 2.3 与 Agent / 模板的对接方式

- **Agent 在进入某个步骤时**，通过 `record_major_step` 写入：
  ```json
  {
    "step_key": "wd.implementing",
    "agent": "web-dev",
    "title": "Web Dev implementing feature",
    "summary_template": "Web Dev updated {files_changed} files on branch {branch_name}.",
    "summary_facts": { "files_changed": 5, "branch_name": "..." },
    "lifecycle_state": "running"
  }
  ```
- **前端渲染时**用 `summary_template` + `summary_facts` 渲染出真正文案。
- **不写死任何用户数据**：模板骨架 + 占位符是唯一的"静态"内容。
- **索引一律使用 `step_instance_key`**：循环和多次暂停必须用 `(step_key, round)` 区分。

---

## 3. 办公任务主要步骤

### 3.1 现状回顾

办公任务（`analyze` / `summarize` / `organize`）当前共用 `office` Agent 的 4 节点工作流（见 `agents/office/agent.py:84` 与 `agents/office/nodes.py`）：

```text
START → receive_task → analyze_request → execute_office_work → report_result → END
```

但 `execute_office_work` 内部的 ReAct 行为按 capability 差异极大：

- `analyze`：读 CSV/XLSX → 推断 schema → 算统计 → 生成分析报告
- `summarize`：读 PDF/DOCX → 总结每个文件 → 多文件时合并
- `organize`：扫描目录 → 规划分组 → 创建文件夹 → 搬文件

Compass 端只记录 4 类粗粒度步骤（`agents/compass/agent.py:506`），导致用户既看不到 Compass→Office 的派发，也看不到 Office 内部针对 capability 差异化的动作。

### 3.2 办公任务骨架分三套

不同 capability 的步骤集不同；同一 capability 内是确定性的固定骨架。

#### 3.2.1 Compass 前置步骤（三个 capability 共用）

| # | step_key | title | actor | conditional |
|---|---|---|---|---|
| 1 | `compass.received` | Compass receiving request | compass | no |
| 2 | `compass.asking_output_mode` | Compass asking for output location | compass | yes (only when not specified) |
| 3 | `compass.dispatched` | Compass dispatching to Office Agent | compass | no |

#### 3.2.2 `analyze` capability（8 步）

| # | step_key | title | actor | what happens |
|---|---|---|---|---|
| 4 | `office.received` | Office receiving task | office | parse message, identify capability & source paths |
| 5 | `office.validating` | Office validating sources and permissions | office | path validation, workspace setup, permission check |
| 6 | `office.inferring_schema` | Office inferring data schema | office | inspect CSV/TSV/XLSX, detect columns and types |
| 7 | `office.computing_stats` | Office computing statistics | office | summary stats for numeric fields, aggregations for categorical fields |
| 8 | `office.generating_report` | Office generating analysis report | office | write the analysis report from inferred schema |
| 9 | `office.writing` | Office writing deliverable | office | write `.analysis.md` for each source (workspace or inplace, see §3.6) |
| 10 | `office.verifying` | Office verifying deliverable | office | check expected outputs exist |
| 11 | `office.delivered` | Office delivering report to Compass | office | write task-report.json + callback |

#### 3.2.3 `summarize` capability（7–8 步，单文件 7 步，多文件 8 步）

| # | step_key | title | actor | what happens |
|---|---|---|---|---|
| 4 | `office.received` | Office receiving task | office | parse message, identify capability & source paths |
| 5 | `office.validating` | Office validating sources and permissions | office | path validation, expand directory inputs |
| 6 | `office.reading` | Office reading documents | office | read PDF/DOCX/TXT/PPTX/CSV/XLSX |
| 7 | `office.summarizing` | Office summarizing each document | office | write per-file summary |
| 8 | `office.combining` | Office creating combined summary | office | **conditional** — only when multiple files |
| 9 | `office.writing` | Office writing deliverable | office | write `.summary.md` for each file + `combined-summary.md` (workspace or inplace, see §3.6) |
| 10 | `office.verifying` | Office verifying deliverable | office | check expected outputs exist |
| 11 | `office.delivered` | Office delivering report to Compass | office | write task-report.json + callback |

#### 3.2.4 `organize` capability（9 步）

| # | step_key | title | actor | what happens |
|---|---|---|---|---|
| 4 | `office.received` | Office receiving task | office | parse message, identify capability & source paths |
| 5 | `office.validating` | Office validating sources and permissions | office | path validation, resource pre-check |
| 6 | `office.scanning` | Office scanning folder structure | office | walk directory, build file inventory with metadata |
| 7 | `office.planning` | Office planning organization | office | choose grouping criteria based on discovered patterns |
| 8 | `office.creating_folders` | Office creating folder structure | office | mkdir under `organized-output/files/` |
| 9 | `office.moving_files` | Office moving files into organized structure | office | copy/move each file to its destination |
| 10 | `office.writing_plan` | Office writing organization plan | office | write `organization-plan.md` to workspace or source folder |
| 11 | `office.verifying` | Office verifying deliverable | office | check organized tree matches canonical inventory |
| 12 | `office.delivered` | Office delivering report to Compass | office | write task-report.json + callback |

### 3.3 动态摘要模板（按 capability 分组）

#### 3.3.1 Compass 前置步骤（三 capability 共用）

| step_key | summary_template | summary_facts |
|---|---|---|
| compass.received | `Compass is preparing your {capability} request for {source_count} {source_kind}.` | `{capability}` `{source_count}` `{source_kind}` |
| compass.asking_output_mode | `Compass is waiting for you to choose the output location.` | — |
| compass.dispatched | `Compass dispatched the task to the Office Agent.` | — |

#### 3.3.2 `analyze` capability 模板

| step_key | summary_template | summary_facts |
|---|---|---|
| office.received | `Office received the task: {capability} on {source_count} {source_kind}.` | `{capability}` `{source_count}` `{source_kind}` |
| office.validating | `Office validated {source_count} {source_kind} and prepared the output area.` | `{source_count}` `{source_kind}` |
| office.inferring_schema | `Office inferred the data schema: {field_count} field(s) detected across {source_count} file(s).` | `{field_count}` `{source_count}` |
| office.computing_stats | `Office computed summary statistics for {numeric_field_count} numeric field(s).` | `{numeric_field_count}` |
| office.generating_report | `Office generated the analysis report from the inferred schema.` | — |
| office.writing | `Office wrote {output_count} analysis report(s) to {output_location}.` | `{output_count}` `{output_location}` |
| office.verifying | `Office verified {output_count} deliverable(s).` | `{output_count}` |
| office.delivered | `Office delivered the report to Compass.` | — |

#### 3.3.3 `summarize` capability 模板

| step_key | summary_template | summary_facts |
|---|---|---|
| office.received | `Office received the task: {capability} on {source_count} {source_kind}.` | `{capability}` `{source_count}` `{source_kind}` |
| office.validating | `Office validated {source_count} {source_kind} and prepared the output area.` | `{source_count}` `{source_kind}` |
| office.reading | `Office read {source_count} {source_kind} via MCP tools.` | `{source_count}` `{source_kind}` |
| office.summarizing | `Office summarized each of the {source_count} document(s).` | `{source_count}` |
| office.combining | `Office created the combined summary covering all {source_count} document(s).` | `{source_count}` |
| office.writing | `Office wrote {output_count} summary file(s) to {output_location}.` | `{output_count}` `{output_location}` |
| office.verifying | `Office verified {output_count} deliverable(s).` | `{output_count}` |
| office.delivered | `Office delivered the report to Compass.` | — |

#### 3.3.4 `organize` capability 模板

| step_key | summary_template | summary_facts |
|---|---|---|
| office.received | `Office received the task: {capability} on {source_count} {source_kind}.` | `{capability}` `{source_count}` `{source_kind}` |
| office.validating | `Office validated {source_count} {source_kind} and prepared the output area.` | `{source_count}` `{source_kind}` |
| office.scanning | `Office scanned the folder and inventoried {file_count} file(s).` | `{file_count}` |
| office.planning | `Office planned the organization around {grouping_criteria}.` | `{grouping_criteria}` |
| office.creating_folders | `Office created the organized folder structure.` | — |
| office.moving_files | `Office placed {file_count} file(s) into their organized locations.` | `{file_count}` |
| office.writing_plan | `Office wrote the organization plan to {output_location}.` | `{output_location}` |
| office.verifying | `Office verified the organized tree matches the canonical inventory.` | — |
| office.delivered | `Office delivered the report to Compass.` | — |

### 3.4 展示示例

#### 任务 1：analyze sales CSV

```text
✓ Compass receiving request                                06-02 09:00:00    0m 24s
                  Compass: Compass is preparing your analyze request for 1 file.
✓ Compass dispatching to Office Agent                      06-02 09:00:24    0m 12s
                  Compass: Compass dispatched the task to the Office Agent.
● Office validating sources and permissions                06-02 09:00:36    Not started yet
                  Office: Office validated 1 file and prepared the output area.
○ Office inferring data schema                             Not started yet.
○ Office computing statistics                              Not started yet.
○ Office generating analysis report                        Not started yet.
○ Office writing deliverable                               Not started yet.
○ Office verifying deliverable                              Not started yet.
○ Office delivering report to Compass                      Not started yet.
```

#### 任务 2：summarize stlouis documents

```text
✓ Compass receiving request                                06-02 09:05:00    0m 24s
                  Compass: Compass is preparing your summarize request for 12 files.
! Compass asking for output location                       ...   ← warn (waiting for user)
                  Compass: Compass is waiting for you to choose the output location.
○ Compass dispatching to Office Agent                      Not started yet.
○ Office receiving task                                    Not started yet.
○ Office validating sources and permissions                Not started yet.
○ Office reading documents                                 Not started yet.
○ Office summarizing each document                         Not started yet.
○ Office creating combined summary                         Not started yet.  (conditional, visible)
○ Office writing deliverable                               Not started yet.
○ Office verifying deliverable                              Not started yet.
○ Office delivering report to Compass                      Not started yet.
```

#### 任务 3：organize 2026 folder

```text
✓ Compass receiving request                                ...
                  Compass: Compass is preparing your organize request for 1 folder.
✓ Compass dispatching to Office Agent                      ...
✓ Office receiving task                                    ...
✓ Office validating sources and permissions                ...
                  Office: Office validated 1 folder and prepared the output area.
● Office scanning folder structure                         ...
                  Office: Office scanned the folder and inventoried 102 file(s).
○ Office planning organization                             Not started yet.
○ Office creating folder structure                         Not started yet.
○ Office moving files into organized structure             Not started yet.
○ Office writing organization plan                         Not started yet.
○ Office verifying deliverable                              Not started yet.
○ Office delivering report to Compass                      Not started yet.
```

### 3.5 与现行实现的差异

| 项 | 现状 | 提案 |
|---|---|---|
| 步骤数 | 2–4 个粗粒度（Compass 端） | analyze 8 步、summarize 8–9 步、organize 9 步（每种能力独立骨架） |
| 标题 | "Office task completed" 这类通用文案 | 每步都是 "actor + action + object"，且与 capability 强相关 |
| 数据来源 | Compass 写入的字符串 | Office Agent 在节点边界写入 `summary_facts` |
| 条件步骤 | 无 | `compass.asking_output_mode`、`office.combining`（仅多文件时） |
| 不同 capability 的差异 | 完全看不出来 | 骨架本身就不一样，差异一目了然 |

### 3.6 `output_mode` 支持：workspace 与 inplace 共享同一份骨架

办公任务支持两种 output mode：

- `workspace`（默认）— 输出写到 `artifacts/{task_id}/office/artifacts/`，源数据保持只读；
- `inplace`（可选，运行时由 `OFFICE_ALLOW_INPLACE_WRITES=true` 开启）— 输出直接写到源文件夹下（如 `<source>/.summary.md`）。

两种 mode **共用同一份步骤骨架**（见 §3.2.2/3.2.3/3.2.4），骨架本身不分支。差异仅体现在：

- step **标题**保持 mode-neutral（如 `Office writing deliverable`，不写"to workspace"）；
- step **摘要模板**通过 `{output_location}` 占位符在运行时解析成 "the workspace" 或 "the source folder"；
- 校验逻辑（`office.verifying`）在 `inplace` 模式下会跳过"输出必须在 workspace 内"的检查。

#### 3.6.1 摘要模板（更新版，支持 `{output_location}`）

以 `analyze` capability 的 `office.writing` 步骤为例：

| step_key | summary_template | summary_facts |
|---|---|---|
| `office.writing` (analyze) | `Office wrote {output_count} analysis report(s) to {output_location}.` | `{output_count}` `{output_location}` |
| `office.writing` (summarize) | `Office wrote {output_count} summary file(s) to {output_location}.` | `{output_count}` `{output_location}` |
| `office.writing` (organize) | `Office placed {file_count} file(s) into their organized locations under {output_location}.` | `{file_count}` `{output_location}` |
| `office.writing_plan` (organize) | `Office wrote the organization plan to {output_location}.` | `{output_location}` |

`{output_location}` 是个**运行时派生的字符串**，由 Office Agent 在 `office.writing` / `office.writing_plan` 节点根据任务元数据中的 `output_mode` 字段填入：

- `output_mode == "workspace"` → `{output_location}` = `"the workspace"`
- `output_mode == "inplace"` → `{output_location}` = `"the source folder"`

#### 3.6.2 同一个骨架两种 mode 下的展示示例

**任务 A：analyze（workspace mode）**

```text
✓ Compass receiving request                                06-02 09:00:00  0m 00s
✓ Compass dispatching to Office Agent                      06-02 09:00:00  0m 00s
✓ Office receiving task                                    06-02 09:00:00  0m 00s
✓ Office validating sources and permissions                06-02 09:00:01  0m 00s
✓ Office inferring data schema                             06-02 09:00:01  0m 12s
✓ Office computing statistics                              06-02 09:00:13  0m 06s
✓ Office generating analysis report                        06-02 09:00:19  0m 18s
● Office writing deliverable                               06-02 09:00:37   running
                  Office: Office wrote 1 analysis report(s) to the workspace.
○ Office verifying deliverable                             Not started yet
○ Office delivering report to Compass                      Not started yet
```

**任务 A'：analyze（inplace mode）**

```text
✓ Compass receiving request                                06-02 09:00:00  0m 00s
✓ Compass dispatching to Office Agent                      06-02 09:00:00  0m 00s
✓ Office receiving task                                    06-02 09:00:00  0m 00s
✓ Office validating sources and permissions                06-02 09:00:01  0m 00s
✓ Office inferring data schema                             06-02 09:00:01  0m 12s
✓ Office computing statistics                              06-02 09:00:13  0m 06s
✓ Office generating analysis report                        06-02 09:00:19  0m 18s
● Office writing deliverable                               06-02 09:00:37   running
                  Office: Office wrote 1 analysis report(s) to the source folder.
○ Office verifying deliverable                             Not started yet
○ Office delivering report to Compass                      Not started yet
```

**任务 B：summarize（inplace mode，多文件 → combining 步骤出现）**

```text
✓ Compass receiving request                                ...
✓ Compass asking for output location                       ...   ← user picked "inplace"
✓ Compass dispatching to Office Agent                      ...
✓ Office receiving task                                    ...
✓ Office validating sources and permissions                ...
✓ Office reading documents                                 ...
✓ Office summarizing each document                         ...
✓ Office creating combined summary                         ...
● Office writing deliverable                               ...
                  Office: Office wrote 13 summary file(s) to the source folder.
○ Office verifying deliverable                             Not started yet
○ Office delivering report to Compass                      Not started yet
```

**任务 C：organize（inplace mode，引用源目录作为输出位置）**

```text
✓ Compass receiving request                                ...
✓ Compass asking for output location                       ...   ← user picked "inplace"
✓ Compass dispatching to Office Agent                      ...
✓ Office receiving task                                    ...
✓ Office validating sources and permissions                ...
✓ Office scanning folder structure                         ...
✓ Office planning organization                             ...
✓ Office creating folder structure                         ...
● Office moving files into organized structure             ...
                  Office: Office placed 102 file(s) into their organized locations under the source folder.
○ Office writing organization plan                         ...   ← title stays mode-neutral; runtime shows "the source folder" via fact
○ Office verifying deliverable                             Not started yet
○ Office delivering report to Compass                      Not started yet
```

> 注意：所有 Office 写入相关 title 必须保持 mode-neutral；输出位置只通过摘要 fact `{output_location}` 表达。

#### 3.6.3 关键不变量

- 步骤骨架条数与顺序**不随 output_mode 变化**——分析 8 步、总结 8–9 步、整理 9 步在两种 mode 下完全一致。
- `compass.asking_output_mode` 步骤的出现条件不受影响：仅在用户**未**指定 workspace/inplace 时才询问；用户已指定 mode（无论哪种）则该步骤保持 `conditional_pending` 状态。
- `office.combining` 步骤的出现条件不受影响：仅在输入多于 1 个文件时出现；与 mode 无关。
- `{output_location}` 派生的字符串**不会**被持久化为独立字段；它是 `summary_facts` 的一部分，每次 `record_major_step` 写入时由 Office Agent 重新计算。

### 3.7 Office 边界场景矩阵

| 场景 | Timeline 行为 | 实现要求 |
|---|---|---|
| 用户请求未指定 output mode | `compass.asking_output_mode#0` 进入 `waiting_for_user` / `warn`；任务状态为 `TASK_STATE_INPUT_REQUIRED` | 用户回复后同一行转 `done`，原 Compass task id 恢复，不创建新顶层 task |
| 用户回复非法 output mode（如 `later`） | 同一 `compass.asking_output_mode` 行保持 `waiting_for_user`，summary 更新为 `Compass is waiting for a valid output location.` | 不派发 Office；记录用户输入到 chat history；继续等待 |
| 用户回复 `workspace` | `compass.asking_output_mode#0` 转 `done`，`compass.dispatched#0` 启动 | `summary_facts.output_location="the workspace"`；源目录必须保持只读 |
| 用户回复 `inplace` 且允许写入 | `compass.asking_output_mode#0` 转 `done`，`compass.dispatched#0` 启动 | `OFFICE_ALLOW_INPLACE_WRITES=true`；`summary_facts.output_location="the source folder"` |
| 用户回复 `inplace` 但策略不允许写入 | `compass.asking_output_mode#0` 继续等待或 `compass.dispatched#0` 标记 `failed`，取决于产品策略 | 必须明确策略：推荐继续等待并提示只能选择 `workspace` |
| Office 运行时发现 inplace 不允许 | `office.validating#0` 或 `office.writing#0` 标记 `failed`，随后 `compass.task_failed#0` | 不允许静默 fallback 到 workspace，除非 summary 明确写出 fallback，且验收测试覆盖 |
| workspace mode 写入失败 | 失败步骤为 `office.writing#0`，终态为 `compass.task_failed#0` | `failure_reason` 必须包含可操作路径或权限原因 |
| inplace mode 写入失败 | 失败步骤为 `office.writing#0` 或 `office.moving_files#0`，终态为 `compass.task_failed#0` | 不修改源文件，或在 summary 中说明已恢复/未恢复 |
| analyze 输入为单文件 | 不出现目录展开相关行；`source_count=1` | 输出文件名保持 `{original_filename}.analysis.md` |
| analyze 输入为目录 | `office.validating#0` 记录展开后的文件数 | 每个可分析文件生成 `{original_filename}.analysis.md`；不可分析文件进入 warning 或 failed 策略 |
| summarize 单文件 | `office.combining#0` 为 `conditional_pending` | 输出 `{original_filename}.summary.md` |
| summarize 多文件 | `office.combining#0` 正常运行 | 输出每文件 summary + `combined-summary.md` |
| organize workspace | `office.moving_files#0` title 仍可用，但语义是 materialize/copy organized output | 源目录不变；验证 `organized-output/files/` 与 canonical inventory 匹配 |
| organize inplace | `office.moving_files#0` 表示源目录内重新组织 | 必须有 inplace 权限；需要记录 backup/restore 或不可恢复变更策略 |
| 下游 Office 完成但 callback 丢失 | Office 本地 `office.delivered#0` 可存在，但 Compass task 不应假装完成 | Compass 通过 polling/report artifact 超时策略写 `compass.task_failed#0` 或 `compass.task_terminated#0` |
| Office 发来迟到事件但 Compass 已终态 | 事件写入 `major_step_events` 且 `ignored_after_terminal=true` | UI 默认不展示该 row，Task Logs 可查 |

### 3.8 Office 扩展规则

新增 Office capability 时，只需要新增该 capability 的 Agent-local skeleton helper 和对应 `record_major_step` 调用。公共规则不变：

- `compass.received` / `compass.asking_output_mode` / `compass.dispatched` 保持共用。
- 写入类步骤必须通过 `{output_location}` 表达 workspace/inplace 差异。
- 验证步骤必须同时覆盖 workspace 输出路径和 inplace 源路径。
- 新 capability 的 step key 使用 `office.<verb_object>`，不要包含 fixture 名、文件名、样例列名或测试任务线索。

---

## 4. 开发任务主要步骤

### 4.1 现状回顾

参考 `docs/development-task-design.md`，节点总数超过 20 个：

- **Compass**: classify / route / dispatch / poll
- **Team Lead**: receive_task / analyze_requirements / gather_context / create_plan / dispatch_dev_agent / review_result / request_revision / pause_for_user_input / report_success
- **Dev Agent (Web)**: prepare_jira / setup_workspace / analyze_task / implement_changes / run_tests / fix_tests / self_assess / fix_gaps / capture_screenshot / create_pr / update_jira / report_result
- **Code Review Agent**: review

而当前 UI 强制展示 7 个固定标题阶段（Plan / Implement / Build / Test / Self-check / Fix / Review & Deliver），靠关键词文本匹配归桶。Fix 与 Test 在 retry 循环下挤在一起。

### 4.2 主要步骤骨架

Actor order: **compass → team-lead → web-dev → code-review → team-lead → compass**

A development task has **two main loops**:

1. **Self-check loop** (inside Web Dev): when self-check fails, run "fix gaps → build/test → self-check" cycle, **can iterate multiple times** until pass.
2. **Code review loop** (coordinated by Team Lead): when review rejects, run "request changes → fix feedback → build/test → self-check → handover → request review → review" cycle, **can iterate multiple times** until pass.

#### 4.2.1 Main flow (happy path, 13 steps)

| # | step_key | title | actor | meaning |
|---|---|---|---|---|
| 1 | `compass.received` | Compass receiving request | compass | receive the Jira ticket request, classify as development |
| 2 | `compass.dispatched` | Compass dispatching to Team Lead | compass | Registry lookup + A2A dispatch to Team Lead |
| 3 | `tl.analyzing` | Team Lead analyzing task | team-lead | task analysis (type / complexity / required skills) |
| 4 | `tl.gathering` | Team Lead gathering requirements | team-lead | fetch Jira / design / clone repo / write context manifest |
| 5 | `tl.dispatched_dev` | Team Lead dispatching to Web Dev | team-lead | hand off context + workspace paths to Web Dev |
| 6 | `wd.drafting_plan` | Web Dev drafting plan | web-dev | read requirements + design, produce implementation plan |
| 7 | `wd.implementing` | Web Dev implementing feature | web-dev | write code + Jira state change + local git setup |
| 8 | `wd.building` | Web Dev building and testing | web-dev | build + run tests (first pass) |
| 9 | `wd.self_check` | Web Dev running self-check | web-dev | check against acceptance criteria + design (first pass) |
| 10 | `wd.handover` | Web Dev handing over to Team Lead | web-dev | self-check passed, deliver to Team Lead |
| 11 | `tl.requesting_review` | Team Lead requesting code review | team-lead | launch Code Review Agent |
| 12 | `cr.reviewing` | Code Review Agent reviewing code | code-review | independent review of the PR |
| 13 | `tl.reported` | Team Lead reporting to Compass | team-lead | review approved, report final result to Compass |

#### 4.2.2 Self-check loop (conditional, runs when #9 fails, **may iterate multiple rounds**)

When #9 self-check fails, the following steps are recorded per round (with `{round}` fact):

| round | step_key | title | actor | meaning |
|---|---|---|---|---|
| N | `wd.fixing_gaps` | Web Dev fixing self-check gaps (round {round}) | web-dev | address self-check gaps |
| N | `wd.rebuilding` | Web Dev rebuilding and retesting (round {round}) | web-dev | rebuild and run tests again |
| N | `wd.self_check_retry` | Web Dev re-running self-check (round {round}) | web-dev | re-run self-check |

- **Pass condition**: self-check passes → jump to #10 `wd.handover`.
- **Loop condition**: self-check fails again → re-enter `wd.fixing_gaps` with `round+1`.
- **Round number**: starts from round 1; each row carries `{round}` fact.

#### 4.2.3 Code review loop (conditional, runs when #12 verdict=rejected, **may iterate multiple rounds**)

When #12 review verdict is `rejected`, the following steps are recorded per round:

| round | step_key | title | actor | meaning |
|---|---|---|---|---|
| N | `tl.requesting_changes` | Team Lead requesting changes from Web Dev (round {round}) | team-lead | aggregate review comments, send to Web Dev |
| N | `wd.addressing_feedback` | Web Dev addressing review feedback (round {round}) | web-dev | fix the review comments |
| N | `wd.rebuilding` | Web Dev rebuilding and retesting (round {round}) | web-dev | rebuild and run tests again |
| N | `wd.self_check_retry` | Web Dev re-running self-check (round {round}) | web-dev | re-run self-check |
| N | `wd.handover_retry` | Web Dev handing over to Team Lead (round {round}) | web-dev | re-deliver to Team Lead |
| N | `tl.re_requesting_review` | Team Lead re-requesting code review (round {round}) | team-lead | re-launch Code Review |
| N | `cr.reviewing_retry` | Code Review Agent re-reviewing code (round {round}) | code-review | re-review the updated PR |

- **Pass condition**: review verdict=approved → jump to #13 `tl.reported`.
- **Loop condition**: review verdict=rejected → re-enter `tl.requesting_changes` with `round+1`.
- **Round number**: starts from round 1; each row carries `{round}` fact.

### 4.3 Dynamic summary templates

| step_key | summary_template | summary_facts |
|---|---|---|
| compass.received | `Compass received the development request for Jira ticket {jira_key}.` | `{jira_key}` |
| compass.dispatched | `Compass dispatched the task to the Team Lead Agent.` | — |
| tl.analyzing | `Team Lead analyzed the request: type={task_type}, complexity={complexity}.` | `{task_type}` `{complexity}` |
| tl.gathering | `Team Lead gathered {jira_count} Jira ticket(s), {design_count} design source(s), and cloned the repository.` | `{jira_count}` `{design_count}` |
| tl.dispatched_dev | `Team Lead dispatched the dev task to the Web Dev Agent.` | — |
| wd.drafting_plan | `Web Dev drafted the implementation plan ({plan_steps} steps).` | `{plan_steps}` |
| wd.implementing | `Web Dev updated {files_changed} files on branch {branch_name}.` | `{files_changed}` `{branch_name}` |
| wd.building | `Web Dev finished build and tests: {test_pass}/{test_total} passed.` | `{test_pass}` `{test_total}` |
| wd.self_check | `Web Dev self-check score: {assess_score}.` | `{assess_score}` |
| wd.handover | `Web Dev handed over the work to Team Lead.` | — |
| tl.requesting_review | `Team Lead launched Code Review Agent for PR {pr_number}.` | `{pr_number}` |
| cr.reviewing | `Code Review completed with verdict: {review_verdict}.` | `{review_verdict}` |
| tl.reported | `Team Lead reported to Compass. PR: {pr_url}.` | `{pr_url}` |
| wd.fixing_gaps | `Web Dev is addressing {gaps_count} self-check gap(s) (round {round}).` | `{gaps_count}` `{round}` |
| wd.rebuilding | `Web Dev re-ran build and tests: {test_pass}/{test_total} passed (round {round}).` | `{test_pass}` `{test_total}` `{round}` |
| wd.self_check_retry | `Web Dev re-ran self-check (round {round}): score {assess_score}.` | `{round}` `{assess_score}` |
| tl.requesting_changes | `Team Lead asked Web Dev to address {review_comments_count} review comment(s) (round {round}).` | `{review_comments_count}` `{round}` |
| wd.addressing_feedback | `Web Dev is addressing review feedback (round {round}).` | `{round}` |
| wd.handover_retry | `Web Dev re-handed over the work to Team Lead (round {round}).` | `{round}` |
| tl.re_requesting_review | `Team Lead re-launched Code Review Agent (round {round}).` | `{round}` |
| cr.reviewing_retry | `Code Review re-completed (round {round}) with verdict: {review_verdict}.` | `{round}` `{review_verdict}` |

### 4.4 Display examples

#### Task A: implement JIRA CSTL-1 (first-try success)

```text
✓ Compass receiving request                                06-02 08:00:00    0m 18s
                  Compass: Compass received the development request for Jira ticket CSTL-1.
✓ Compass dispatching to Team Lead                         06-02 08:00:18    0m 06s
                  Compass: Compass dispatched the task to the Team Lead Agent.
✓ Team Lead analyzing task                                 06-02 08:00:30    0m 24s
                  Team Lead: Team Lead analyzed the request: type=feature, complexity=medium.
✓ Team Lead gathering requirements                         06-02 08:00:54    1m 12s
                  Team Lead: Team Lead gathered 1 Jira ticket(s), 1 design source(s), and cloned the repository.
✓ Team Lead dispatching to Web Dev                         06-02 08:02:06    0m 06s
                  Team Lead: Team Lead dispatched the dev task to the Web Dev Agent.
✓ Web Dev drafting plan                                    06-02 08:02:16    0m 18s
                  Web Dev: Web Dev drafted the implementation plan (4 steps).
✓ Web Dev implementing feature                             06-02 08:02:36    32m 00s
                  Web Dev: Web Dev updated 5 files on branch feature/cstl-1-login.
✓ Web Dev building and testing                             06-02 08:34:36    4m 00s
                  Web Dev: Web Dev finished build and tests: 42/43 passed.
✓ Web Dev running self-check                               06-02 08:38:36    0m 30s
                  Web Dev: Web Dev self-check score: 0.93.
✓ Web Dev handing over to Team Lead                        06-02 08:39:06    0m 06s
                  Web Dev: Web Dev handed over the work to Team Lead.
✓ Team Lead requesting code review                         06-02 08:39:12    0m 12s
                  Team Lead: Team Lead launched Code Review Agent for PR 42.
✓ Code Review Agent reviewing code                         06-02 08:39:24    5m 00s
                  Code Review: Code Review completed with verdict: approved.
✓ Team Lead reporting to Compass                           06-02 08:44:24    0m 24s
                  Team Lead: Team Lead reported to Compass. PR: https://scm/.../pull-requests/42.
```

#### Task B: self-check fails 2 rounds, then passes

```text
... (same as Task A's #1–#7)
✓ Web Dev building and testing                             ... 42/43 passed
✕ Web Dev running self-check                               ... 0.72   ← failed
✓ Web Dev fixing self-check gaps (round 1)                 06-02 08:39:00    6m 00s
                  Web Dev: Web Dev is addressing 2 self-check gap(s) (round 1).
✓ Web Dev rebuilding and retesting (round 1)               06-02 08:45:00    3m 00s
                  Web Dev: Web Dev re-ran build and tests: 43/43 passed (round 1).
✕ Web Dev re-running self-check (round 1)                  ... 0.84   ← failed again
✓ Web Dev fixing self-check gaps (round 2)                 06-02 08:48:00    4m 00s
                  Web Dev: Web Dev is addressing 1 self-check gap(s) (round 2).
✓ Web Dev rebuilding and retesting (round 2)               06-02 08:52:00    2m 00s
                  Web Dev: Web Dev re-ran build and tests: 43/43 passed (round 2).
✓ Web Dev re-running self-check (round 2)                  06-02 08:54:00    0m 24s
                  Web Dev: Web Dev re-ran self-check (round 2): score 0.95.
✓ Web Dev handing over to Team Lead                        ... (continues with #11–#13)
```

#### Task C: code review fails 1 round, then passes

```text
... (same as Task A's #1–#12, but #12 verdict=rejected)
✕ Code Review Agent reviewing code                         ... rejected
✓ Team Lead requesting changes from Web Dev (round 1)     06-02 09:30:00    0m 12s
                  Team Lead: Team Lead asked Web Dev to address 3 review comment(s) (round 1).
✓ Web Dev addressing review feedback (round 1)             06-02 09:30:12    14m 00s
                  Web Dev: Web Dev is addressing review feedback (round 1).
✓ Web Dev rebuilding and retesting (round 1)               06-02 09:44:12    5m 00s
                  Web Dev: Web Dev re-ran build and tests: 43/43 passed (round 1).
✓ Web Dev re-running self-check (round 1)                  06-02 09:49:12    0m 30s
                  Web Dev: Web Dev re-ran self-check (round 1): score 0.95.
✓ Web Dev handing over to Team Lead (round 1)              06-02 09:49:42    0m 06s
                  Web Dev: Web Dev re-handed over the work to Team Lead (round 1).
✓ Team Lead re-requesting code review (round 1)            06-02 09:49:48    0m 12s
                  Team Lead: Team Lead re-launched Code Review Agent (round 1).
✓ Code Review Agent re-reviewing code (round 1)            06-02 09:50:00    4m 00s
                  Code Review: Code Review re-completed (round 1) with verdict: approved.
✓ Team Lead reporting to Compass                           ...
```

### 4.5 Failure scenarios

| Failure point | Timeline representation |
|---|---|
| Compass dispatch failure | `compass.dispatched` marked `failed`; task terminates |
| Team Lead gathering failure (Jira unreachable / Repo clone fail) | `tl.gathering` marked `failed`; later steps stay pending |
| Web Dev build / tests exhausted retries | `wd.building` marked `failed`; task terminates |
| Self-check fails and gap-fix exhausted | `wd.fixing_gaps` marked `failed` (last failed round); task terminates |
| Code Review `max_revisions` exhausted | `cr.reviewing` / `cr.reviewing_retry` marked `failed`; Team Lead rollback |
| Final report failure | `tl.reported` marked `warn` (degradable); Task Logs explain |

### 4.6 Difference vs current implementation

| Item | Current | Proposed |
|---|---|---|
| Step count | fixed 7 stages | 13 main-flow steps + 3 steps per self-check round + 7 steps per code review round + user-input steps |
| Title style | abstract noun (Plan / Implement / Build / Test) | actor + action + object (e.g. "Web Dev building and testing") |
| Categorization | keyword matching (fragile) | explicit `step_key` from backend (deterministic) |
| Retry representation | Build/Test/Fix visually squashed | each loop step is its own row, with `round` number and own timestamps |
| Failure representation | all stages marked red | `failedStepInstanceKey` points to the stuck step |
| Loops | not first-class | both loops are first-class, may iterate unlimited rounds |
| User input requests | not first-class (only Compass) | any Agent can request user input; rendered as `<agent_name> requesting user input for <reason>` row |

### 4.7 User input requests are first-class major steps

Any Agent in the development task flow may need to pause and ask the user for clarification. Per `docs/development-task-design.md` §3.1, when an Agent requires user input it returns `TASK_STATE_INPUT_REQUIRED`, the user responds in Compass, and the original task is resumed (no new top-level task is created). This lifecycle should be **visible in the timeline** as a dedicated row, because it is one of the most informative events a user can see ("the agent is blocked on me").

#### 4.7.1 General pattern

| step_key | title (template) | actor (template) | conditional |
|---|---|---|---|---|
| `<agent_prefix>.requesting_user_input` | `<dev_agent_name> requesting user input for <reason>` | the requesting agent | yes (only when this happens) |

- `step_key` is parameterized by the requesting Agent's prefix (`tl.*` / `wd.*` / `cr.*`); multiple Agents can each have their own row in the same timeline.
- The title's `<reason>` is the **specific reason** the Agent is asking, e.g. `"ambiguous acceptance criteria"`, `"which repo to clone"`, `"how to handle failing integration test"`. It is injected at call time via `summary_facts.input_reason`.
- The same Agent may request user input **multiple times** (e.g., Team Lead asks for clarification, then asks again later). Each occurrence increments `round` and renders as its own row.
- State lifecycle: `waiting_for_user` (paused) → `resuming` (user replied, workflow restarting) → `done` (the paused edge has been resolved). The single row covers the waiting phase and freezes once the user response is accepted; when waiting, the row shows the `warn` visual.

#### 4.7.2 Concrete step_key / title values for dev agents

| Agent | step_key | example title (runtime-resolved) |
|---|---|---|
| Team Lead | `tl.requesting_user_input` | `Team Lead requesting user input for ambiguous requirements` |
| Web Dev | `wd.requesting_user_input` | `Web Dev requesting user input for unclear acceptance criteria` |
| Code Review | `cr.requesting_user_input` | `Code Review requesting user input for priority of suggested fix` |

(For office tasks, the equivalent is `compass.asking_output_mode` from §3.2.1 and the same pattern applies to any future Agent that pauses for the user.)

#### 4.7.3 Where in the dev flow this can fire

The pause-for-input can fire at any node. The most common cases per `docs/development-task-design.md`:

| Where in dev flow | step_key | example input_reason |
|---|---|---|
| Team Lead `analyze_requirements` ambiguity | `tl.requesting_user_input` | `"ambiguous requirements"` |
| Team Lead `gather_context` blocked (no repo URL, multiple repos) | `tl.requesting_user_input` | `"which repository to clone"` |
| Web Dev `implement_changes` blocked on choice | `wd.requesting_user_input` | `"unclear acceptance criteria"` |
| Web Dev `self_assess` needs more context | `wd.requesting_user_input` | `"how to weight component checks"` |
| Code Review needs priority guidance | `cr.requesting_user_input` | `"priority of suggested fixes"` |

#### 4.7.4 Summary template

| step_key | summary_template | summary_facts |
|---|---|---|
| `tl.requesting_user_input` | `Team Lead requested user input: {input_reason}; user response received after waiting {wait_duration}.` | `{input_reason}` `{wait_duration}` |
| `wd.requesting_user_input` | `Web Dev requested user input: {input_reason}; user response received after waiting {wait_duration}.` | `{input_reason}` `{wait_duration}` |
| `cr.requesting_user_input` | `Code Review requested user input: {input_reason}; user response received after waiting {wait_duration}.` | `{input_reason}` `{wait_duration}` |

- `{wait_duration}` is formatted in the same `Xh Ym Zs` style as the time-spent column (e.g. `12m 00s`, `1h 23m 04s`).
- The waiting duration is computed as the elapsed time from when the Agent first calls `record_major_step(..., lifecycle_state="waiting_for_user", ...)` to when it (or the resume handler) calls again with `lifecycle_state="done"`.
- The waiting duration is **also reflected in the right-hand column** of the timeline row, in addition to being part of the summary. The format on the right is `06-02 08:06:01  12m 00s` (where `12m 00s` is the wait). This way the user sees the wait at a glance, even without reading the summary line.
- If the user has not yet responded, the template renders without the trailing "user response received" — `lifecycle_state` drives that suffix in the renderer, and the right-hand time column continues to tick.

#### 4.7.5 Display example: Web Dev asking for clarification, then completing the task

```text
✓ Compass receiving request                                          06-02 08:00:00  0m 00s
✓ Compass dispatching to Team Lead                                   06-02 08:00:00  0m 00s
✓ Team Lead analyzing task                                           06-02 08:00:01  0m 24s
✓ Team Lead gathering requirements                                   06-02 08:00:25  1m 12s
✓ Team Lead dispatching to Web Dev                                   06-02 08:01:37  0m 06s
✓ Web Dev drafting plan                                              06-02 08:01:43  0m 18s
✓ Web Dev implementing feature                                       06-02 08:02:01  4m 00s
! Web Dev requesting user input for unclear acceptance criteria       06-02 08:06:01  12m 00s  ← warn (waiting on user, 12m 00s of wait)
                  Web Dev: Web Dev requested user input: unclear acceptance criteria.
                  (user replied in chat at 08:18:01)
✓ Web Dev building and testing                                       06-02 08:24:01  4m 00s
✓ Web Dev running self-check                                         06-02 08:28:01  0m 30s
✓ Web Dev handing over to Team Lead                                  06-02 08:28:31  0m 06s
... (continues with review flow)
```

#### 4.7.6 Multiple rounds in the same task

If the same Agent requests user input twice, `round` differentiates them:

```text
! Web Dev requesting user input for missing field validation rule (round 1)   06-02 08:06:01  12m 00s
✓ Web Dev building and testing                                                 06-02 08:24:01  4m 00s
! Web Dev requesting user input for test framework choice (round 2)             06-02 08:32:01  6m 00s
✓ Web Dev building and testing (round 2)                                       06-02 08:42:01  4m 00s
```

#### 4.7.7 Acceptance for user-input steps

- [ ] Any Agent can call `record_major_step` with `step_key="<prefix>.requesting_user_input"`, `lifecycle_state="waiting_for_user"`, `summary_facts={"input_reason": "..."}`.
- [ ] When the user responds, the same Agent (or Compass on resume) calls `record_major_step` again with the same `step_key`, same `round`, and `lifecycle_state="done"`; the row is updated, not duplicated.
- [ ] A second pause in the same task uses `round=2` (or higher) and renders as a new row.
- [ ] The renderer shows the row with the `warn` visual while `lifecycle_state="waiting_for_user"`, and `done` after the user responds.
- [ ] Office tasks continue to use the existing `compass.asking_output_mode` (no change).

---

## 5. Common UI behavior

### 5.1 Default collapse

- Default collapse follows the pointer priority in §0.6.
- Top retains `Show all steps` / `Current step only` toggle button (consistent with current behavior).

### 5.2 Status semantics

| Visual state | Meaning |
|---|---|
| `done` | step finished successfully |
| `current` | step is executing |
| `warn` | step is still running but a degradable warning appeared (waiting for user, assignee update fail) |
| `failed` | step terminated with failure |
| `pending` | not yet reached |
| `conditional_pending` | conditional step; may or may not be reached in this task |

### 5.3 Loop step rendering

- Steps triggered by loops (self-check / code review loop) display by default once they fire.
- Each round is one row, with `(round N)` suffix so iteration count is obvious.
- Multiple rounds are not collapsed; a thin separator visually groups "loop" context.

### 5.4 Boundary with Task Logs

- **Timeline does not show sub-step details**. For example `wd.building` does not expand into npm / vitest / playwright.
- Users who want details: click a step → open Task Logs (with agent filter pre-selected to that step's agent).
- This keeps 13+ steps from drowning the timeline.

---

## 6. Data contract

### 6.1 New task metadata fields

Compass 顶层 task metadata 是 UI 唯一数据源。新字段如下：

```json
{
  "task_type": "development",
  "office_capability": "",
  "active_step_instance_key": "wd.implementing#0",
  "last_step_instance_key": "wd.implementing#0",
  "failed_step_instance_key": "",
  "terminal_step_instance_key": "",
  "major_step_events": [
    {
      "event_id": "evt-001",
      "task_id": "task-abc",
      "orchestrator_task_id": "task-abc",
      "step_key": "wd.implementing",
      "step_instance_key": "wd.implementing#0",
      "round": 0,
      "title": "Web Dev implementing feature",
      "agent": "web-dev",
      "lifecycle_state": "running",
      "visual_state": "current",
      "summary_template": "Web Dev updated {files_changed} files on branch {branch_name}.",
      "summary_facts": { "files_changed": 5, "branch_name": "feature/cstl-1-login" },
      "created_at": "2026-06-02T08:02:36+00:00"
    }
  ],
  "major_step_rows": [
    {
      "step_key": "wd.implementing",
      "step_instance_key": "wd.implementing#0",
      "round": 0,
      "title": "Web Dev implementing feature",
      "agent": "web-dev",
      "lifecycle_state": "running",
      "visual_state": "current",
      "conditional": false,
      "summary_template": "Web Dev updated {files_changed} files on branch {branch_name}.",
      "summary_facts": { "files_changed": 5, "branch_name": "feature/cstl-1-login" },
      "started_at": "2026-06-02T08:02:36+00:00",
      "ended_at": null,
      "ignored_after_terminal": false
    }
  ],
  "major_step_skeleton": [
    {
      "step_key": "wd.implementing",
      "step_instance_key": "wd.implementing#0",
      "round": 0,
      "title": "Web Dev implementing feature",
      "agent": "web-dev",
      "conditional": false
    }
  ],
  "step_states": {
    "wd.implementing#0": {
      "lifecycle_state": "running",
      "visual_state": "current",
      "started_at": "2026-06-02T08:02:36+00:00",
      "ended_at": null
    }
  },
  "step_summaries": {
    "wd.implementing#0": {
      "summary_template": "Web Dev updated {files_changed} files on branch {branch_name}.",
      "summary_facts": { "files_changed": 5, "branch_name": "feature/cstl-1-login" }
    }
  }
}
```

兼容输出：

- `majorSteps` 映射自 `major_step_rows`。
- `majorStepsSkeleton` 映射自 `major_step_skeleton`。
- `currentMajorStepKey` 迁移期保留，但值来自 §0.6 默认折叠选择逻辑，不再等同于最新写入事件。
- `progressSteps` 迁移期继续输出，用作旧 UI fallback。

### 6.2 Common `record_major_step` signature

```python
def record_major_step(
    task_id: str,
    *,
    step_key: str,
    title: str,
    agent: str,
    lifecycle_state: str = "running",
    visual_state: str | None = None,
    summary_template: str = "",
    summary_facts: dict | None = None,
    round: int = 0,
    conditional: bool = False,
    orchestrator_task_id: str = "",
    progress_sink: object | None = None,
    task_store: TaskStore | None = None,
) -> dict:
    ...
```

Rules:

- `step_instance_key` is derived from `(step_key, round)` inside the function.
- The function always appends a `major_step_event`.
- The function updates or creates one `major_step_rows` row with the same `step_instance_key`.
- If `lifecycle_state` is `waiting_for_user`, the row has no `ended_at`.
- If `lifecycle_state` is `done` / `failed` / `cancelled` / `terminated`, the row has `ended_at`.
- If `orchestrator_task_id` differs from `task_id`, the function also emits the event through `progress_sink` so Compass can merge it into the top-level task.

### 6.3 Frontend rendering flow

```text
rows = task.majorStepRows || task.majorSteps || []
skeleton = task.majorStepsSkeleton || []

if expanded:
  visible = merge rows with unfired skeleton rows
else:
  key = task.activeStepInstanceKey
     || task.failedStepInstanceKey
     || task.terminalStepInstanceKey
     || task.lastStepInstanceKey
  visible = rows where row.step_instance_key == key

for each row in visible:
  summary = render_template(row.summary_template, row.summary_facts)
  render timeline-row with row.title, row.visual_state, summary, started_at, duration
```

Template substitution rules:

- Placeholder `{name}` -> `facts[name]`.
- Missing value renders as `--`, never `undefined`.
- All values pass through `escape` to prevent XSS.
- User-provided free text in `summary_facts` must be length-capped before rendering.

---

## 7. Migration path

### 7.1 Phase 1: data plumbing (UI unchanged)

- Add `framework/major_step.py` with `record_major_step` and the merge helper that updates `major_step_events`, `major_step_rows`, `major_step_skeleton`, `step_states`, and pointer fields.
- Replace the legacy Compass-local major-step helper writes with the common API while continuing to emit legacy `progress_steps` during migration.
- Teach downstream Agents to call the common API at node boundaries and propagate events back to the Compass top-level task through the authorized progress channel.
- Old and new data coexist; timeline still uses keyword bucketing as fallback but writes structured step data.

### 7.2 Phase 2: UI switch

- Frontend `deriveMajorPhases` switches to read `majorStepRows` / `majorStepsSkeleton` and uses `step_instance_key` for all row identity.
- Keyword bucketing is demoted to fallback.
- Office capability-specific skeletons are enabled.
- Waiting-for-user rows use `lifecycle_state="waiting_for_user"` and `visual_state="warn"`.

### 7.3 Phase 3: cleanup

- Remove `developmentPhaseForText` and the keyword bucketing code in `deriveMajorPhases`.
- Drop the legacy `progressSteps` field (keep 1–2 versions for compatibility).
- Remove the legacy Compass-local helper after all callers use `framework.major_step.record_major_step`.

---

## 8. Acceptance checklist

### 8.1 Display format

- [ ] All implementation values (step_key, title, summary_template, summary_facts, agent, status) are in **English**.
- [ ] Timeline cards show `MM-DD HH:MM:SS` start time and a concise `Xs` / `Xm Ys` / `Xh Ys` / `Xh Ym Zs` time spent on a single inline row (no separate "Started" / "Time Spent" labels).
- [ ] **Unfired steps render `--` for both STARTED and TIME SPENT** (consistent placeholder convention). The literal text `Not started yet` is **removed** as of v0.8.1.
- [ ] **Compact duration rules** (revised 2026-06-03): omit leading zero units, e.g. `53s`, `6m 12s`, `1h 5s`. A duration of `0` hides the Time Spent pill entirely (no `0s` rendered).
- [ ] **Skipped-row fix** (revised 2026-06-03): an unfired skeleton row whose position is **before** a later fired row is rendered as `visual_state=done` (instead of `pending`) so the timeline reflects the real execution order — no `pending → done → pending → done` pattern.
- [ ] **Panel-head alignment** (revised 2026-06-03): the three panel-heads (`Task List`, `Compass Chat`, `Task Info`) share a single-line header with ellipsis on the task id; a long task id does **not** push the bottom border down out of alignment with the other two panels.

### 8.2 Office task capability coverage

- [ ] The 3 office tasks (analyze / summarize / organize) each render their own capability-specific timeline.
- [ ] The `summarize` timeline shows `office.combining` only when input has more than one file; otherwise it stays in `conditional_pending`.
- [ ] `compass.asking_output_mode` shows only when the user did not pre-specify output location.
- [ ] Both `output_mode=workspace` and `output_mode=inplace` use the same step skeleton; only the `summary_facts.output_location` value differs.
- [ ] Invalid output-mode replies keep the same Compass task in `TASK_STATE_INPUT_REQUIRED`; no Office dispatch happens until the user gives `workspace` or `inplace`.
- [ ] `inplace` requested while writes are not allowed is handled explicitly: either continue waiting with a clear summary or fail before Office dispatch; no silent fallback.
- [ ] Office `analyze` / `summarize` / `organize` each have tests for both workspace and inplace mode.
- [ ] Workspace mode never mutates the source tree; inplace mode records permission, backup/restore, or non-recoverable mutation policy in the timeline/logs.

### 8.3 Development task coverage

- [ ] The development timeline shows 13 main-flow steps; when self-check fails, 3 extra steps appear per round with the round number; when code review fails, 7 extra steps appear per round with the round number.
- [ ] Self-check failing 2 times and code review failing 1 time are both rendered as distinct rounds with correct timestamps and facts.
- [ ] Any Agent (Team Lead / Web Dev / Code Review) can call `record_major_step` with `step_key="<prefix>.requesting_user_input"`, `lifecycle_state="waiting_for_user"`, `summary_facts={"input_reason": "..."}`; the row appears as `<agent_name> requesting user input for <reason>` with the `warn` visual.
- [ ] When the user responds, the same row transitions to `lifecycle_state="done"` (same `step_instance_key` collapses to a single row, not a duplicate).
- [ ] A second pause in the same task uses `round=2` and renders as a new row.
- [ ] Resume keeps the same top-level Compass task id and the same workspace path.

### 8.4 Dev agent extensibility

- [ ] Adding a new `dev_agent_type` (e.g. `android-dev`) requires zero changes to `framework/major_step.py` and zero changes to the Compass UI.
- [ ] The new dev agent's `dev_agent_type → prefix / name` mapping is owned by the agent itself; Team Lead only forwards `dev_agent_type` in the A2A message metadata.
- [ ] Skeleton rows are task-local and specific to the selected `dev_agent_type`; rows from other dev-agent families never appear in this task.
- [ ] The new dev agent can introduce its own extra `step_key` (e.g. `id.submitting_to_testflight`) without framework or UI changes; the auto-skeleton behavior (§10.5) registers it on first call.

### 8.5 Data contract & UI

- [ ] No hard-coded user data (Jira key, file path, PR URL, product name) anywhere in titles, templates, or placeholders.
- [ ] Keyword bucketing (`developmentPhaseForText` etc.) is replaced by backend-explicit `step_key`.
- [ ] A failed task's `failedStepInstanceKey` points exactly to the stuck step; `terminalStepInstanceKey` points to the final Compass terminal row.
- [ ] Loop rows and repeated user-input rows are indexed by `step_instance_key`, so round 1 and round 2 never overwrite each other.
- [ ] Conditional steps not triggered in this task render in the `conditional_pending` visual state.
- [ ] Task Logs still provide full sub-step diagnostic information for any step the user clicks.
- [ ] Default Compass UI view follows the §0.6 pointer priority; clicking "Show all steps" reveals every row in `majorStepRows` plus unfired conditional skeleton rows.
- [ ] Downstream Agent steps recorded in isolated task stores are visible on the Compass top-level task through the approved progress propagation path.
- [ ] Once a terminal row is written, later normal events are marked `ignored_after_terminal=true` and do not appear in the default UI.

---

## 9. Open questions for follow-up

### 9.1 Resolved in v0.6

- ✅ Each loop round renders as its own row with `(round N)` suffix. Confirmed.
- ✅ Conditional steps in happy path render as `conditional_pending` (semi-transparent). Confirmed.
- ✅ Live E2E: `tests/e2e/test_timeline_major_steps_e2e.py` to be added. Confirmed.
- ✅ For `analyze` capability, `field_count` / `numeric_field_count` facts come from the Office Agent's schema inference stats. Confirmed.
- ✅ For `organize` capability, `grouping_criteria` fact comes from the discovered grouping strategy. Confirmed.
- ✅ User input steps display waiting time via `{wait_duration}` fact, and waiting time is also reflected in the right-hand time column of the row. See §4.7.4.
- ✅ Terminal events (completed / failed / terminated / cancelled) are first-class major steps recorded by Compass. See §12.

### 9.2 Still open for follow-up

- Should the summary line of a `compass.task_failed` / `terminated` / `cancelled` step link directly to the relevant `Task Logs` segment, so the user can jump from "why did it fail?" to the full diagnostic in one click?
- When a task is paused on user input, should the right-hand time column continue to tick (live update) or freeze at the value when `lifecycle_state="waiting_for_user"` was first set? Current proposal: live tick while waiting, freeze on `done`.
- For tasks with very long `wait_duration` (e.g. user replied after 1 day), should we cap the displayed wait at e.g. `>1d` to avoid visual noise?
- Should the per-step `failed` row's title include a short error code (e.g. `[PERM_DENIED]`) in addition to the long reason, for quick scanning?

---

## 10. Common API: `record_major_step`

本节定义公共函数的实现契约。字段语义以 §0 和 §6 为准。

### 10.1 Module location

新增 `framework/major_step.py`，与 `framework/devlog.py`、`framework/checkpoint.py` 并列。

理由：

- 这是任务级步骤事件，不是日志，也不是 checkpoint。
- 所有 Agent 都依赖，应放在 framework 共享层。
- 单独模块便于单元测试、契约验证和跨 Agent 复用。

### 10.2 Required constants

```python
LIFECYCLE_PENDING = "pending"
LIFECYCLE_CONDITIONAL_PENDING = "conditional_pending"
LIFECYCLE_RUNNING = "running"
LIFECYCLE_WAITING_FOR_USER = "waiting_for_user"
LIFECYCLE_RESUMING = "resuming"
LIFECYCLE_DONE = "done"
LIFECYCLE_WARNING = "warning"
LIFECYCLE_FAILED = "failed"
LIFECYCLE_CANCELLED = "cancelled"
LIFECYCLE_TERMINATED = "terminated"

VISUAL_PENDING = "pending"
VISUAL_CONDITIONAL_PENDING = "conditional_pending"
VISUAL_CURRENT = "current"
VISUAL_WARN = "warn"
VISUAL_DONE = "done"
VISUAL_FAILED = "failed"
```

`visual_state` 默认由 `lifecycle_state` 派生。调用方只有在需要特殊呈现时才传入 `visual_state`。

### 10.3 Function signature

```python
def record_major_step(
    task_id: str,
    *,
    step_key: str,
    title: str,
    agent: str,
    lifecycle_state: str = "running",
    visual_state: str | None = None,
    summary_template: str = "",
    summary_facts: dict | None = None,
    round: int = 0,
    conditional: bool = False,
    orchestrator_task_id: str = "",
    progress_sink: object | None = None,
    task_store: TaskStore | None = None,
) -> dict:
    """Record a major workflow step event and update the corresponding UI row."""
```

Validation:

- `task_id`, `step_key`, `title`, and `agent` are required.
- `round` must be an integer >= 0.
- `summary_facts` must be JSON-serializable and must not contain secrets.
- `step_key` and `agent` values are English identifiers.
- `title` and `summary_template` are English user-visible strings.

### 10.4 Merge behavior

```text
step_instance_key = f"{step_key}#{round}"

append event to major_step_events
if terminal row already exists and this event is not terminal:
  append event with ignored_after_terminal=true
  do not update major_step_rows
else:
  create or update major_step_rows[step_instance_key]
  create or update major_step_skeleton[step_instance_key]
  update step_states[step_instance_key]
  update step_summaries[step_instance_key]
  update active/failed/terminal/last pointer fields
```

`major_step_events` is append-only. `major_step_rows` is idempotent on `step_instance_key`.

### 10.5 Cross-agent propagation

When an Agent owns the Compass task store, `record_major_step` updates Compass metadata directly.

When an Agent has an isolated task store, the call must still create the same event locally and then propagate it back to Compass:

```python
record_major_step(
    state["_task_id"],
    orchestrator_task_id=state.get("_compass_task_id", ""),
    progress_sink=state.get("_major_step_progress_sink"),
    step_key="office.writing",
    title="Office writing deliverable",
    agent="office",
    lifecycle_state="running",
)
```

The progress sink may be an A2A callback, a Capability Registry capability, or an in-process test sink. It must be supplied by the orchestrator or registry-resolved metadata; Agent code must not hardcode Compass URLs.

### 10.6 Usage examples

Compass receiving an Office request:

```python
record_major_step(
    task.id,
    step_key="compass.received",
    title="Compass receiving request",
    agent="compass",
    lifecycle_state="running",
    summary_template="Compass is preparing your {capability} request for {source_count} {source_kind}.",
    summary_facts={
        "capability": capability,
        "source_count": len(source_paths),
        "source_kind": "file" if len(source_paths) == 1 else "files",
    },
)
```

Compass waiting for output mode:

```python
record_major_step(
    task.id,
    step_key="compass.asking_output_mode",
    title="Compass asking for output location",
    agent="compass",
    lifecycle_state="waiting_for_user",
    conditional=True,
    summary_template="Compass is waiting for you to choose the output location.",
)
```

Office writing a deliverable:

```python
record_major_step(
    state["_task_id"],
    orchestrator_task_id=state.get("_compass_task_id", ""),
    progress_sink=state.get("_major_step_progress_sink"),
    step_key="office.writing",
    title="Office writing deliverable",
    agent="office",
    lifecycle_state="running",
    summary_template="Office wrote {output_count} analysis report(s) to {output_location}.",
    summary_facts={
        "output_count": output_count,
        "output_location": "the workspace" if output_mode == "workspace" else "the source folder",
    },
)
```

Web Dev self-check retry:

```python
record_major_step(
    state["_task_id"],
    step_key="wd.self_check_retry",
    title=f"Web Dev re-running self-check (round {self_check_round})",
    agent="web-dev",
    lifecycle_state="running",
    round=self_check_round,
    conditional=True,
)
```

### 10.7 UI consumption

The UI reads only `GET /api/tasks/{task_id}` from Compass. `agents/compass/ui/routes.py` exposes:

```python
{
    "activeStepInstanceKey": metadata.get("active_step_instance_key", ""),
    "lastStepInstanceKey": metadata.get("last_step_instance_key", ""),
    "failedStepInstanceKey": metadata.get("failed_step_instance_key", ""),
    "terminalStepInstanceKey": metadata.get("terminal_step_instance_key", ""),
    "majorStepRows": list(metadata.get("major_step_rows") or []),
    "majorStepEvents": list(metadata.get("major_step_events") or []),
    "majorSteps": list(metadata.get("major_step_rows") or []),
    "majorStepsSkeleton": list(metadata.get("major_step_skeleton") or []),
    "stepStates": dict(metadata.get("step_states") or {}),
    "stepSummaries": dict(metadata.get("step_summaries") or {}),
    "progressSteps": list(metadata.get("progress_steps") or []),
}
```

Default rendering uses the pointer priority in §0.6. Expanded rendering merges `majorStepRows` with unfired `majorStepsSkeleton` rows and sorts by row insertion order, not by localized display time.

### 10.8 Acceptance for the API

- [ ] `framework/major_step.py` exists and exports `record_major_step`.
- [ ] Calling the function with `(task_id, step_key, title, agent)` validates the four required fields.
- [ ] Every call appends one `major_step_events` entry.
- [ ] Two calls with the same `(step_key, round)` update the same `major_step_rows` entry.
- [ ] Two calls with the same `step_key` and different `round` create two distinct `step_instance_key` rows.
- [ ] `waiting_for_user` rows do not get `ended_at` until the user response is accepted.
- [ ] `done` / `failed` / `cancelled` / `terminated` rows get `ended_at`.
- [ ] Terminal rows block later normal rows from appearing in `major_step_rows`.
- [ ] Downstream isolated Agent events are merged into the Compass top-level task.
- [ ] No Agent code outside `framework/major_step.py` writes directly to timeline metadata fields.

### 10.9 Why this design

- **One writer, many readers**: Agents call the API; UI reads the standard task endpoint.
- **Event/row separation**: diagnostics stay append-only while UI rows stay stable.
- **Idempotent on `step_instance_key`**: enter/leave calls update one row; loop rounds remain separate.
- **Compass-owned UI source of truth**: Office/dev subtasks can run in isolated stores without hiding their progress from Compass.
- **Pointer separation**: active, failed, terminal, and last-written steps no longer fight over one current-step field.

---

## 11. Extending dev skeleton to android-dev / ios-dev (and beyond)

开发任务目前以 `web-dev` 为唯一 Dev Agent 实现。设计目标之一是**未来加入 `android-dev` / `ios-dev` / `backend-dev` 等其他 Dev Agent 时，不修改时间线骨架**——只是在 Agent 端按运行时确定的 `dev_agent_type` 用同一套 step_key 调用 `record_major_step`。

### 11.1 The dev agent family

| dev_agent_type | agent value (in `record_major_step`) | human-readable name (used in titles) | step_key prefix |
|---|---|---|---|---|
| `web-dev` (current) | `web-dev` | `Web Dev` | `wd.*` |
| `android-dev` (planned) | `android-dev` | `Android Dev` | `ad.*` |
| `ios-dev` (planned) | `ios-dev` | `iOS Dev` | `id.*` |
| `backend-dev` (planned) | `backend-dev` | `Backend Dev` | `bd.*` |
| ... | ... | ... | ... |

The step_key prefix is a short Agent-owned identifier. The table above is documentation only, not a central runtime registry. When a new dev agent is added, that agent owns its own `dev_agent_type -> prefix / display name` mapping and emits normal `record_major_step` events.

### 11.2 The 13-step main skeleton is **shared** across all dev agent types

The main-flow skeleton (see §4.2.1) is generic. The only thing that varies between `web-dev` / `android-dev` / `ios-dev` is:

1. The `step_key` prefix (`wd.` / `ad.` / `id.`).
2. The `agent` value (`web-dev` / `android-dev` / `ios-dev`).
3. The `title` field — written using a `{dev_agent_name}` placeholder so the title is independent of the agent type.
4. The `summary_facts` fields — agent-specific data (e.g., web has `branch_name`, android might have `apk_path`).

Concretely, the skeleton for **any** dev agent type looks like this:

| # | step_key (template) | title (template) | agent (template) |
|---|---|---|---|
| 1 | `{prefix}.received` | `{dev_agent_name} receiving dev request` | (set by dev agent on first call) |
| 6 | `{prefix}.drafting_plan` | `{dev_agent_name} drafting plan` | … |
| 7 | `{prefix}.implementing` | `{dev_agent_name} implementing feature` | … |
| 8 | `{prefix}.building` | `{dev_agent_name} building and testing` | … |
| 9 | `{prefix}.self_check` | `{dev_agent_name} running self-check` | … |
| 10 | `{prefix}.handover` | `{dev_agent_name} handing over to Team Lead` | … |

`{prefix}` and `{dev_agent_name}` are resolved by the dev agent itself at the moment it calls `record_major_step`, using the runtime value of `dev_agent_type` (which is decided by Team Lead when it dispatches).

### 11.3 Concrete example: an `android-dev` task

Suppose Team Lead decided the task is an Android bug fix and dispatches to `android-dev`. The dev agent resolves:

```python
DEV_AGENT_TYPE = "android-dev"     # injected at construction time
DEV_AGENT_PREFIX = "ad"            # looked up from a small table
DEV_AGENT_NAME = "Android Dev"     # looked up from a small table
```

When `android-dev` calls `record_major_step`, it produces the following timeline:

```text
✓ Compass receiving request                                       06-02 08:00:00  0m 00s
✓ Compass dispatching to Team Lead                                06-02 08:00:00  0m 00s
✓ Team Lead analyzing task                                        06-02 08:00:01  0m 24s
✓ Team Lead gathering requirements                                06-02 08:00:25  1m 12s
✓ Team Lead dispatching to Android Dev                            06-02 08:01:37  0m 06s
✓ Android Dev drafting plan                                       06-02 08:01:43  0m 18s
✓ Android Dev implementing feature                                06-02 08:02:01  32m 00s
                  Android Dev: Android Dev updated 7 files on branch fix/cstl-2-crash.
✓ Android Dev building and testing                                06-02 08:34:01  4m 00s
                  Android Dev: Android Dev finished build and tests: 18/18 unit tests passed; lint clean.
✓ Android Dev running self-check                                  06-02 08:38:01  0m 30s
                  Android Dev: Android Dev self-check score: 0.91.
✓ Android Dev handing over to Team Lead                           06-02 08:38:31  0m 06s
✓ Team Lead requesting code review                                06-02 08:38:37  0m 12s
✓ Code Review Agent reviewing code                                06-02 08:38:49  5m 00s
                  Code Review: Code Review completed with verdict: approved.
✓ Team Lead reporting to Compass                                  06-02 08:43:49  0m 24s
                  Team Lead: Team Lead reported to Compass. PR: https://scm/.../pull-requests/43.
```

The skeleton is **the same 13 rows** as for `web-dev`; only the human-readable names and the agent value differ.

### 11.4 What Team Lead needs to do

Team Lead already has `dev_agent_type` available (it picks the agent when constructing the delivery plan, see `docs/development-task-design.md` §5.3). The only addition is:

- When Team Lead dispatches to a dev agent, it passes `dev_agent_type` in the A2A message metadata.
- The dev agent reads `dev_agent_type` from message metadata and uses it to derive `prefix` / `dev_agent_name` locally.
- Team Lead's own steps (analyzing, gathering, requesting review, reporting) are unchanged — they don't depend on the dev agent type.

No timeline registry file needs to be updated when a new dev agent is added. Capability/agent routing still uses the existing Capability Registry; the timeline layer only requires the new agent to emit valid step events.

### 11.5 What if a new dev agent needs a different step shape?

The skeleton in §4.2.1 is the **shared baseline** that 80% of dev tasks follow. If a future dev agent needs an extra step (e.g., `ios-dev` adds `ios.submitting_to_testflight`), it does so by calling `record_major_step` with a new `step_key`; the function automatically registers the new task-local row in `task.metadata.major_step_skeleton`.

The UI renders rows from `major_step_rows` plus task-local `major_step_skeleton` entries for `conditional_pending` rows. So adding an extra step is a **dev-agent-local change**, not a framework or UI change.

### 11.6 What if a future dev agent needs *fewer* steps?

Same answer: the dev agent simply doesn't call `record_major_step` for the steps it doesn't have. Skeleton metadata is task-local; it must never inherit rows from previous tasks or other dev-agent families. Two options:

- **Option A (recommended)**: keep only the baseline skeleton for the selected `dev_agent_type` and render explicitly declared-but-unfired conditional rows as `conditional_pending`.
- **Option B**: have the dev agent write a `skeleton_overrides` list in its first call to `record_major_step`, telling the API "remove these step_keys from the skeleton for this task". This is more complex but produces a tighter timeline.

For v0.8 we adopt **Option A**; if a future dev agent's skipped rows prove noisy, we can switch to Option B without changing the public API.

### 11.7 Summary: what changes when adding a new dev agent

| Change needed | Where | Cost |
|---|---|---|
| `agent_id` registration in registry | `agents/<new>/config.yaml` + registry bootstrap | one-time |
| `dev_agent_type → prefix / name` mapping | inside the new dev agent's own code | ~5 lines |
| First call to `record_major_step` from each node | inside the new dev agent's own code | one call per node |
| **No change** to `framework/major_step.py` | — | 0 |
| **No change** to `agents/compass/ui/templates.py` | — | 0 |
| **No change** to the skeleton table in §4.2.1 | — | 0 |

---

## 12. Terminal event major steps (completed / failed / terminated / cancelled)

A task always ends in one of four terminal states. Each terminal state should appear as its own **final row** in the timeline so the user can see at a glance how the task ended, and the reason for non-success endings.

The four terminal states:

| Terminal state | Meaning | Who records the step |
|---|---|---|
| `completed` | task ran to its natural success end | the Agent that produced the final deliverable (e.g. `tl.reported` for dev, `office.delivered` for office) — already covered by existing steps |
| `failed` | task ran but could not produce the final deliverable | the Agent that hit the unrecoverable error, **plus** Compass at the very end (so the timeline always ends with a `compass.task_failed` row) |
| `terminated` | task was forcefully killed by an operator (SIGTERM, container stop, watchdog timeout) | Compass (the orchestrator), on receiving the kill signal |
| `cancelled` | user explicitly cancelled the task (e.g. clicked "Cancel" in Compass UI) | Compass, on receiving the user's cancel action |

### 12.1 Step keys for terminal events

| step_key | title (template) | actor | lifecycle_state | visual_state |
|---|---|---|---|
| `compass.task_completed` | `Compass marking task completed` | compass | `done` | `done` |
| `compass.task_failed` | `Compass marking task failed: {failure_reason}` | compass | `failed` | `failed` |
| `compass.task_terminated` | `Compass marking task terminated: {termination_reason}` | compass | `terminated` | `failed` |
| `compass.task_cancelled` | `Compass marking task cancelled by user` | compass | `cancelled` | `failed` |

`compass.task_completed` is a no-op for happy-path dev tasks (the existing `tl.reported` step is already the completion signal), but the Agent that delivers the final result still calls `record_major_step` with `lifecycle_state="done"` so the prior step (e.g. `tl.reported`) is properly closed. The dedicated `compass.task_completed` row is **optional** — it only fires if Compass wants to add a summary-level completion line.

`compass.task_failed` / `task_terminated` / `task_cancelled` are **always** fired when applicable, so the timeline never silently ends mid-flow.

### 12.2 Summary templates

| step_key | summary_template | summary_facts |
|---|---|---|
| `compass.task_completed` | `Compass marked the task as completed.` | — |
| `compass.task_failed` | `Compass marked the task as failed: {failure_reason}.` | `{failure_reason}` |
| `compass.task_terminated` | `Compass marked the task as terminated: {termination_reason}.` | `{termination_reason}` |
| `compass.task_cancelled` | `Compass marked the task as cancelled by user: {cancel_reason}.` | `{cancel_reason}` |

- `{failure_reason}` is the most informative error string the orchestrator can produce (e.g. `"max_revisions exhausted at Code Review round 3"`, `"Jira ticket CSTL-1 not accessible"`).
- `{termination_reason}` is what the watchdog/operator reports (e.g. `"container received SIGTERM"`, `"watchdog timeout after 1800s"`).
- `{cancel_reason}` is the user's free-text reason if they provided one; falls back to `"no reason provided"`.

### 12.3 Interaction with per-step failure

A per-step `failed` state (e.g. `wd.building` marked `failed`) is **different** from a task-level `failed` event:

- A per-step `failed` is the Agent's own acknowledgement that its node ran into a non-recoverable error.
- A `compass.task_failed` is the orchestrator's acknowledgement that the **whole task** ended in failure.

The two can coexist: the per-step `failed` row tells the user **which step** failed, and the `compass.task_failed` row tells them **the task was marked failed as a result**.

### 12.4 Display examples

**Office task: completed (happy path)**

```text
✓ Compass receiving request                                06-02 09:00:00  0m 00s
✓ Compass dispatching to Office Agent                      06-02 09:00:00  0m 00s
✓ Office receiving task                                    06-02 09:00:01  0m 00s
✓ Office validating sources and permissions                06-02 09:00:01  0m 00s
✓ Office inferring data schema                             06-02 09:00:01  0m 12s
✓ Office computing statistics                              06-02 09:00:13  0m 06s
✓ Office generating analysis report                        06-02 09:00:19  0m 18s
✓ Office writing deliverable                               06-02 09:00:37  0m 06s
✓ Office verifying deliverable                             06-02 09:00:43  0m 00s
✓ Office delivering report to Compass                      06-02 09:00:43  0m 00s
```

(no `compass.task_completed` row — the existing final `office.delivered` step is the closing event.)

**Office task: failed (workspace write blocked)**

```text
✓ Compass receiving request                                06-02 09:00:00  0m 00s
✓ Compass dispatching to Office Agent                      06-02 09:00:00  0m 00s
✓ Office receiving task                                    06-02 09:00:01  0m 00s
✓ Office validating sources and permissions                06-02 09:00:01  0m 00s
✓ Office inferring data schema                             06-02 09:00:01  0m 12s
✓ Office computing statistics                              06-02 09:00:13  0m 06s
✓ Office generating analysis report                        06-02 09:00:19  0m 18s
✕ Office writing deliverable                               06-02 09:00:37  0m 06s   ← per-step failure
                  Office: Office wrote 0 analysis report(s); write failed: permission denied on /artifacts/.../office/artifacts/.
✕ Compass marking task failed: write failed: permission denied on /artifacts/.../office/artifacts/.  06-02 09:00:43  0m 06s
                  Compass: Compass marked the task as failed: write failed: permission denied on /artifacts/.../office/artifacts/.
```

**Dev task: code review max_revisions exhausted (failed)**

```text
... (early steps done, several self-check and code review rounds elapsed)
✕ Code Review Agent re-reviewing code (round 3)            06-02 11:42:00  4m 00s
                  Code Review: Code Review re-completed (round 3) with verdict: rejected.
✕ Compass marking task failed: max_revisions exhausted at Code Review round 3.  06-02 11:46:00  0m 06s
                  Compass: Compass marked the task as failed: max_revisions exhausted at Code Review round 3.
```

**Dev task: user cancelled mid-implementation**

```text
✓ Compass receiving request                                06-02 08:00:00  0m 00s
✓ Compass dispatching to Team Lead                         06-02 08:00:00  0m 00s
✓ Team Lead analyzing task                                 06-02 08:00:01  0m 24s
✓ Team Lead gathering requirements                         06-02 08:00:25  1m 12s
✓ Team Lead dispatching to Web Dev                         06-02 08:01:37  0m 06s
✓ Web Dev drafting plan                                    06-02 08:01:43  0m 18s
✕ Web Dev implementing feature                             06-02 08:02:01  0m 04s   ← closed to cancelled (round 0) before terminal row
✕ Compass marking task cancelled by user: changed requirements, no need to continue.  06-02 08:02:05  0m 04s
                  Compass: Compass marked the task as cancelled by user: changed requirements, no need to continue.
```

**Dev task: container SIGTERM during long build (terminated)**

```text
... (build started)
● Web Dev building and testing                             06-02 10:00:00  0m 22m
✕ Compass marking task terminated: container received SIGTERM from orchestrator (task_id=task-cstl-1).  06-02 10:22:00  0m 00s
                  Compass: Compass marked the task as terminated: container received SIGTERM from orchestrator (task_id=task-cstl-1).
```

### 12.5 Acceptance for terminal steps

- [ ] On happy path completion, the final existing step (e.g. `tl.reported` / `office.delivered`) is marked `done`; the timeline ends there.
- [ ] On task failure, the per-step failure row is preserved AND a final `compass.task_failed` row is appended with a non-empty `failure_reason`.
- [ ] On user cancellation, a `compass.task_cancelled` row is appended with the user's `cancel_reason` (or `"no reason provided"` if empty).
- [ ] On operator termination (SIGTERM, watchdog), a `compass.task_terminated` row is appended with the `termination_reason`.
- [ ] The terminal row is always the **last visible** row in the timeline; later events can be appended to `major_step_events` only with `ignored_after_terminal=true`.
- [ ] The terminal row uses the `failed` visual state (so the user sees the task is in a non-success terminal state); `compass.task_completed` is the only terminal row that uses `done`.

---

## 13. Document review and known gaps (backlog)

This section is a self-review of v0.8. It enumerates inconsistencies, missing cases, and edge cases that the implementation team should be aware of. Each item is tagged with a priority and a suggested target version.

| Tag | Meaning |
|---|---|
| `[blocker]` | Must be resolved before implementation starts. |
| `[v0.8.x]` | Should be fixed in a v0.8 patch release; affects implementation correctness. |
| `[v0.9]` | Track as backlog; can ship v0.8 implementation without it. |

### 13.1 Group A: internal conflicts (blocker / v0.8.x)

#### A1. Mode-neutral title inconsistency in `organize` example
- **Location**: §3.2.4 row `office.writing_plan` says "Office writing organization plan to workspace", but §3.6.2 任务 C example shows the same step as "Office writing organization plan" (no "to workspace").
- **Conflict**: §0.8 mode-neutral principle says titles must not mention mode; the §3.2.4 row violates this.
- **Fix**: change §3.2.4 row title to `Office writing organization plan`. The runtime fact `{output_location}` carries the mode-specific phrasing.
- **Priority**: `[blocker]`

#### A2. Missing `resuming` state in user-input lifecycle
- **Location**: §4.7.1 states `waiting_for_user` → `resuming` → `done`, but §4.7.4 example calls only use `lifecycle_state="waiting_for_user"` and `"done"`. §0.5 has a constant `LIFECYCLE_RESUMING = "resuming"` but no consumer.
- **Conflict**: if `resuming` is not used, the §0.5 enum has a dead value; if it is used, the §4.7.4 examples are wrong.
- **Fix**: explicitly define the call pattern. Recommended: `waiting_for_user` (set by Agent on pause) → `resuming` (set by Compass on resume) → `done` (set by Agent after resume succeeds). Document this in §4.7.4 and add the `lifecycle_state="resuming"` call in the example.
- **Priority**: `[blocker]`

#### A3. Terminal-protection example doesn't close the in-flight step
- **Location**: §12.4 cancellation example shows `wd.implementing` still in `●` (running) visual state, with the terminal row written after.
- **Conflict**: §0.7 says terminal rows close the task; the in-flight `wd.implementing` row should transition to `cancelled` lifecycle_state before the terminal row is written.
- **Fix**: in §12.4 examples, the in-flight step should be `✕` with lifecycle_state=`cancelled` (or `terminated` / `failed`) and the terminal row should follow.
- **Priority**: `[blocker]`

#### A4. `{prefix}.received` in dev agent skeleton has no example
- **Location**: §11.2 table lists `{prefix}.received` as step #1 for dev agents, but §11.3 `android-dev` example has no `ad.received` row.
- **Conflict**: the table claims a step the example doesn't show.
- **Fix**: either (a) explicitly mark `{prefix}.received` as optional and remove from the table, or (b) add an `ad.received` row to the §11.3 example to match.
- **Priority**: `[v0.8.x]`

#### A5. Self-check "step-failed-but-task-continues" uses `✕` visual
- **Location**: §4.2.2 self-check loop says the loop continues after a failed self-check. §4.4 example shows the failed self-check row as `✕` (failed visual), which suggests task termination to users.
- **Conflict**: `✕` (per §0.5) is the visual for `lifecycle_state="failed"`, which historically meant the task is in a failed terminal state. Using it for an intermediate step that the task recovers from is misleading.
- **Fix**: use `!` (warn visual) for self-check rows that are about to be retried. Only the final `compass.task_failed#0` should use `✕`.
- **Priority**: `[v0.8.x]`

#### A6. `compass.task_completed` is both "optional" and "always"
- **Location**: §12.1 says `compass.task_completed` is optional; §12.5 says on happy path the final step (e.g. `tl.reported`) is marked `done` and the timeline ends there.
- **Conflict**: if `compass.task_completed` never fires, the timeline "ends" on `tl.reported`, which conflicts with the principle that a compass-orchestrated task should have a single closing event. If it always fires, "optional" is wrong.
- **Fix**: pick one. Recommended: **always fire** `compass.task_completed#0` on happy path. This keeps the closing event consistent across all task types (failed/cancelled/terminated all have a terminal row; completed should too).
- **Priority**: `[v0.8.x]`

### 13.2 Group B: missing cases (blocker / v0.9)

#### B1. User cancels while waiting for user input
- **Missing scenario**: §4.7 covers "user responds" and "waiting" but not "user cancels during wait".
- **Required behavior**: `wd.requesting_user_input#0` should transition to `lifecycle_state="cancelled"`, then `compass.task_cancelled#0` should be written.
- **Fix**: add a paragraph in §4.7 + an example in §12.4.
- **Priority**: `[blocker]`

#### B2. Office task failure does not show `compass.task_failed#0`
- **Missing scenario**: §12.4 only has one office failure example and it doesn't show the `compass.task_failed#0` terminal row.
- **Required behavior**: every office task failure (whether at `office.writing#0` or `office.verifying#0` or earlier) must be followed by `compass.task_failed#0`.
- **Fix**: extend §12.4 office failure example to include the `compass.task_failed#0` row.
- **Priority**: `[blocker]`

#### B3. `major_step_events` grows unbounded
- **Missing**: §6.1 introduces `major_step_events` as an append-only event log, but no retention policy.
- **Required behavior**: a long task with many self-check retries could produce thousands of events.
- **Fix**: add a retention rule to §6.1. Recommended: keep all events in memory for the duration of the task; at task termination, compact to a single summary event per `step_instance_key` (keep first enter, last exit, terminal reason). Alternative: hard cap at N=200 events with the oldest dropped and replaced with `[compacted]`.
- **Priority**: `[v0.9]`

#### B4. Concurrency / atomicity of `record_major_step`
- **Missing**: §10.4 says "merge" but doesn't specify atomicity. Two parallel calls (e.g., Compass writing `compass.dispatched#0` and a downstream Agent writing `tl.analyzing#0` almost simultaneously) could lose updates.
- **Fix**: in §10.4, require the merge to use TaskStore's atomic update (or a per-task lock if no atomic update is available).
- **Priority**: `[v0.9]`

#### B5. `progress_sink` must resolve through Capability Registry
- **Missing**: §10.5 says the sink "must be supplied by the orchestrator or registry-resolved metadata" but doesn't enforce it.
- **Required behavior**: per `CLAUDE.md`, "all inter-agent communication must resolve the target through the Capability Registry first".
- **Fix**: in §10.5, explicitly require `progress_sink` to be obtained from a registered capability (e.g., `compass.major_step.sink`). Document the new capability registration in §11 of `docs/development-task-design.md` or a sibling doc.
- **Priority**: `[blocker]`

#### B6. Empty source / oversized file in office tasks
- **Missing**: §3.2 has no handling for empty input or single very large file.
- **Required behavior**:
  - Empty source → `office.validating#0` records `source_count=0`, no later steps, terminal `compass.task_failed#0` with `failure_reason="no source files"`.
  - Oversized file → `office.summarizing#0` may run for >1h; user sees a single "running" row for hours. Optional: emit `lifecycle_state="warning"` with `summary_facts={"progress_pct": 42}` every N minutes.
- **Fix**: add a subsection in §3 (or §3.7 boundary matrix) for these cases.
- **Priority**: `[v0.9]`

#### B7. Schema evolution
- **Missing**: no policy for adding new fields to the major step event/row schema in the future.
- **Fix**: add a one-paragraph "schema evolution" rule: new fields are additive; old readers ignore unknown fields; renaming or removing fields requires a deprecation cycle of one minor version.
- **Priority**: `[v0.9]`

#### B8. Backward compat: what value does `currentMajorStep` carry during migration?
- **Missing**: §6.1 says it's kept during migration but doesn't say what value.
- **Fix**: define `currentMajorStep = title of row[active_step_instance_key]`. Old readers that only know `currentMajorStep` get a usable value, but cannot tell the difference between running / failed / done.
- **Priority**: `[v0.8.x]`

#### B9. Retry / rerun semantics
- **Missing**: no clear answer on whether a user-initiated retry creates a new task_id (fresh timeline) or reuses the existing one.
- **Recommendation**: new task_id (clean timeline); the old task_id remains in the task list as a "superseded" task. Add a `superseded_by_task_id` pointer on the old task.
- **Priority**: `[v0.9]`

### 13.3 Group C: edge cases (v0.9)

#### C1. Self-check "step-failed-but-task-continues" visual
- Same as A5. Tracked separately to ensure the design intent is clear: failed intermediate steps use `!` (warn) not `✕` (failed).

#### C2. `record_major_step` when task does not exist
- Currently §10.3 raises `KeyError`. Consider silent no-op + log + return `None` for testing convenience. Document the chosen behavior.
- **Priority**: `[v0.9]`

#### C3. `task_id` vs `orchestrator_task_id` distinction
- When are they different? Currently the rule in §0.2 is "must use Compass task id", which makes `orchestrator_task_id == task_id` in most cases. The distinction is only meaningful for cross-task-store propagation. Add a 1-2 sentence clarification.
- **Priority**: `[v0.9]`

#### C4. Wait-tick precision in UI
- When a step is in `waiting_for_user`, the right-hand time column "continues to tick" (per §0.5). At what interval? 1s / 5s / 10s? Overhead considerations for many concurrent waiting tasks.
- **Recommendation**: tick at 5s; sleep at 30s+; fine-tune after profiling.
- **Priority**: `[v0.9]`

#### C5. Re-attach after server restart
- After a Compass restart, in-flight `active_step_instance_key` may point to a step whose Agent is also restarting. Re-attach policy: re-emit the step as `lifecycle_state="warning"` with `summary_facts={"reattach": true}` so the UI knows the step is no longer trust-worthy.
- **Priority**: `[v0.9]`

### 13.4 Summary

| Group | Items | Blockers | v0.8.x | v0.9 |
|---|---|---|---|---|
| A: internal conflicts | 6 | 3 | 3 | 0 |
| B: missing cases | 9 | 4 | 1 | 4 |
| C: edge cases | 5 | 0 | 0 | 5 |
| **Total** | **20** | **7** | **4** | **9** |

**Recommendation**: address the 7 blockers (A1, A2, A3, B1, B2, B5, plus the choice on A6) before implementation starts. The remaining 4 v0.8.x items can ship in a v0.8.1 patch once the implementation team has exercised the API in real tasks. The 9 v0.9 items are pure backlog and can be tracked via this §13.

---

## 14. v0.8.1 patch: UI alignment + display format fixes

Resolved 2026-06-03 against the running Compass UI. All four items were reported by users after a real E2E run with the v0.8 implementation.

### 14.1 UI #1 — `Task Info` panel-head bottom separator alignment

**Symptom**: when a task id is present in the `Task Info` panel-head, the bottom border of that panel-head was rendered at a different y-position than the `Task List` and `Compass Chat` panel-heads, breaking the three-column header alignment.

**Root cause**: `.panel-head-title` had `flex-wrap: wrap`, and the long task id (rendered in a monospace font) wrapped to a second line, pushing the bottom border down by ~16 px. The other two panel-heads had no second row of content, so they stayed at the original y.

**Fix** (`agents/compass/ui/templates.py`):
- `.panel-head-title` → `flex-wrap: nowrap; overflow: hidden;`
- `.panel-head strong` → `white-space: nowrap; flex-shrink: 0;`
- `.detail-head-task-id` → `max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;`

**Effect**: a long task id is truncated with an ellipsis inside a single-line header; the three panel-head borders line up exactly.

### 14.2 UI #2 — `Time Spent` should use a concise form

**Symptom**: short durations rendered with leading zeros (`00m 12s`, `00h 01m 05s`) made the column visually noisy and inconsistent.

**Root cause**: the original `compactDuration` always zero-padded minutes and seconds and always emitted the `h` segment, which is the format required for column alignment in dense tables but feels verbose for sparse timelines.

**Fix** (`agents/compass/ui/templates.py` `compactDuration`):

| Input | Old output | New output |
|---|---|---|
| 0 ms | `00m 00s` | `''` (pill hidden) |
| 53 s | `00m 53s` | `53s` |
| 6 m 12 s | `06m 12s` | `6m 12s` |
| 1 h 5 s | `01h 00m 05s` | `1h 5s` |
| 1 h 23 m 4 s | `01h 23m 04s` | `1h 23m 4s` |

The new rule: **omit leading zero units; a duration of zero hides the pill entirely**. The caller (`timelineHtmlForMajorSteps`) now checks `durationLabel === ''` and omits the `<span class="timeline-fact">…Time Spent…</span>` element rather than rendering an empty placeholder.

### 14.3 UI #3 — middle steps showing `not started yet` while later steps are completed

**Symptom**: in expanded view, an unfired skeleton row (no event ever recorded) appeared with the `pending` mark and `Not started yet` text even though a later row in the same task had already reached `done`. The visual ordering `○ pending → ✓ done → ○ pending → ✓ done` contradicted the actual execution order.

**Root cause**: `deriveMajorTimeline` only had a special case for the legacy `compass.received` row (`isLegacyCompassReceived`); all other unfired rows were rendered as `pending` regardless of whether a later row had fired.

**Fix** (`agents/compass/ui/templates.py` `deriveMajorTimeline`): generalize the `hasLaterFiredStep` check. Any unfired row (skeleton row with no `major_step_rows` entry) whose position is **before** a later fired row is flipped to `visual_state=done` so the timeline reflects the real execution order. `lifecycle_state` is left empty to indicate no event was recorded (distinguishable from a real completion in tests / dashboards).

**Effect**: `pending → done` no longer appears between fired rows; the expanded timeline is now a monotonic visual sequence.

### 14.4 UI #4 — `Not started yet` → `--`

**Symptom**: unfired rows showed the literal text `Not started yet` for the STARTED label, while the TIME SPENT label showed `--` (or `00m 00s`). The two placeholders were inconsistent.

**Fix** (`agents/compass/ui/templates.py`): unfired rows now render `--` for both STARTED and TIME SPENT. The literal string `Not started yet` is removed entirely.

### 14.5 Backlog items closed by this patch

- `C1` (self-check step-failed-but-task-continues visual) — closed by the earlier `LIFECYCLE_WARNING` change in the v0.8 baseline.
- Skeleton-row `pending` rendering (UI #3) — closed by §14.3.
- Compact duration format (UI #2) — closed by §14.2.
- `Not started yet` literal (UI #4) — closed by §14.4.
- Panel-head alignment (UI #1) — closed by §14.1.

### 14.6 Tests added

- `test_render_ui_renders_compact_duration_for_v08_timeline` — updated to assert the four concise branches and the zero-hide behaviour.
- `test_render_ui_marks_skipped_skeleton_rows_as_done_when_later_steps_fired` — covers §14.3.
- `test_render_ui_uses_dash_placeholder_for_unfired_started_label` — covers §14.4.
- `test_render_ui_panel_head_does_not_wrap_with_long_task_id` — covers §14.1.
