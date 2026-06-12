# Office/Compass 澄清回复契约（2026-06-11）

## 背景

当前 Office 的澄清流程混合了两类本应分离的职责：

1. **展示 / 时间线身份**：UI 应该把哪一条 major-step 行保持为等待用户回复。
2. **回复语义**：这一轮澄清里，系统实际上期待用户给出什么类型的回答。

这种耦合很脆弱。一个面向展示的 interrupt kind 可能会在同一条多阶段澄清链路里承载多个语义子模式（例如“选择维度”和“审批草拟计划”）。系统随后只能依赖 `interrupt.kind`、`needs_clarification.missing` 和局部关键词解析的临时组合去推断含义。这会让行为难以推理、容易回归，而且一旦需要扩展，就只能继续添加更多按 case 分支的逻辑。

这个方法论缺口并不只存在于某一个 organize 场景。任何会因为用户输入而暂停的 agent，都需要一个显式契约，说明当前期待的回复类型、如何解释该回复，以及在什么情况下系统必须 fail closed 而不是自行猜测。

## 目标

1. **让回复理解显式化。** 每一个澄清 payload 都必须声明当前预期用户回复的语义契约。
2. **把 UI 连续性和语义校验分离。** 同一条时间线 row 可以跨越多个澄清阶段保持稳定，而不必强迫这些阶段复用同一个回复解析器。
3. **使用统一的解析路径。** Compass 和 Office 必须依赖同一个基于契约的 resolver，而不是各自复制一套回复启发式逻辑。
4. **在歧义时 fail closed。** 如果回复不满足当前契约，任务保持在 `INPUT_REQUIRED`，并向用户发出精确的重问；系统不得静默重解释用户回复。
5. **保持方法级通用性。** 该设计不能把具体文件夹 schema、领域名词或任务特例编码进运行时行为。

## 非目标

- 不重做 Compass 的聊天 UI 或 major-step 视觉设计，除非为了保持正确性所需的最小元数据调整。
- 不为每一次澄清回复都引入基于 LLM 的自由意图分类。
- 不修改与澄清无关的 Office 执行逻辑。
- 本轮不扩展到 Team Lead、Dev 或 Code Review agent 流程，尽管这套契约模型后续应可复用于这些 agent。

## 根因

当前流程把 `interrupt.kind` 同时用于两种职责：

- 选择用户可见的等待 row 与 resume 路由；
- 隐式选择回复解析器。

只有当“一条等待 row 恰好对应一种回复语义”时，这种做法才成立。一旦同一条 row 可能覆盖多个澄清阶段，解析逻辑就必须额外去看 `needs_clarification.missing`、嵌入的计划状态、原始文本等信息，于是逻辑分层叠加并逐渐脆弱。

更深层的问题是方法论上的：**系统没有把“当前什么样的回复才算有效”视为一等状态。**

## 推荐方案

为每个 `needs_clarification` payload 引入显式的 **reply contract（回复契约）**，并把它持久化到任务的 interrupt metadata 中。reply contract 成为校验和标准化的唯一真源。`interrupt.kind` 仍然保留，用于时间线 / UI 的连续性，但它不再负责语义解析。

## 架构

### 新的数据拆分

每个 waiting-for-user 状态都保留两个独立字段：

- `interrupt.kind`
  作用：展示身份、major-step 连续性、粗粒度路由。
- `needs_clarification.reply_contract`
  作用：对下一条用户回复进行语义校验和标准化。

示例结构：

```python
{
    "kind": "office_organize_dimension",
    "needs_clarification": {
        "missing": "organizeCustomPlan",
        "user_message": "...",
        "options": [...],
        "reply_contract": {
            "schema_version": 1,
            "kind": "approve_or_modify",
            "actions": [
                {"id": "approve", "label": "Approve plan"},
                {"id": "modify", "label": "Modify plan", "requires_note": False},
            ],
            "free_text_suffix": "optional",
            "reask_message": "Please reply with `approve` or `modify: <change>`.",
            "ambiguity_policy": "reask",
        },
    },
}
```

关键点在于：时间线仍然可以保持一条稳定的 `office_organize_dimension` row，但语义契约已经明确说明当前期待的是“审批动作”，而不是“选择维度”。

### 共享回复解析器

新增共享模块，例如 `framework/clarification_reply.py`，导出：

- `resolve_reply(contract: Mapping[str, Any], user_text: str) -> ReplyResolution`
- `render_reask(contract, reason) -> str`
- 对支持的 contract kind 进行校验的辅助函数

`ReplyResolution` 应该是结构化结果：

```python
{
    "ok": True,
    "normalized": {
        "kind": "approve_or_modify",
        "action": "modify",
        "note": "create top-level buckets first",
    },
    "diagnostic": "matched_action_prefix",
}
```

或者：

```python
{
    "ok": False,
    "reason": "unknown_action",
    "reask_message": "Please reply with `approve` or `modify: <change>`.",
}
```

Compass 用这个 resolver 做前置校验和用户可见的重问。Office 在等待中的任务恢复时，使用同一个 resolver 做权威标准化。对于同一个 contract kind，系统中不得存在两套不同的语义解析器。

### 本轮支持的 contract kinds

本轮只需要一组很小、但通用的 contract kind：

- `select_option`
  用于回复必须解析成某一个规范 option id 的场景。
- `approve_or_modify`
  用于二选一动作回复，后面可带可选或必填的补充文本。
- `free_text`
  用于直接接受原始文本的场景。

contract kind 是方法级概念。具体任务只提供 option 列表和消息，而不新增专属解析逻辑。

### 标准化后的澄清 payload

在收到有效回复后，Compass 把标准化结果写入一个通用字段，例如：

```python
office_request["clarification_resolution"] = {
    "contract_kind": "approve_or_modify",
    "action": "approve",
    "note": "",
}
```

Office 随后在恢复同一个任务时消费这个标准化 payload。为了兼容灰度期间的旧逻辑，Office 内部仍可继续填充诸如 `organize_custom_action` 这样的 legacy 字段，但这些字段应当只在本地转换步骤中出现，而不再作为跨 agent 契约。

## 流程变化

### 变更前

1. Office 以 `needs_clarification` 暂停。
2. Compass 把 `missing` 映射成某个 `interrupt.kind`。
3. 恢复时，Compass 主要依据 `interrupt.kind` 选择解析器，并附加少量特殊分支。
4. Office 再用自己的本地逻辑重新解析同一条回复。

### 变更后

1. Office 以带有 `needs_clarification.reply_contract` 的 payload 暂停。
2. Compass 原样保存该 payload，同时保留自己选择的 `interrupt.kind`，仅用于 UI 连续性。
3. 恢复时，Compass 按当前激活的 `reply_contract` 解析用户回复。
4. 对无效或有歧义的回复，使用同一份契约进行重问。
5. 对有效回复，把标准化后的澄清结果转发给同一个等待中的 Office session。
6. Office 使用同一类共享 resolver 契约和标准化 payload，而不是再从原始文本和本地分支中猜测。

## 组件变更

### `framework/clarification_reply.py`（新增）

- 定义 contract schema 辅助方法和共享 resolver。
- 维护类似 `approve_or_modify` 这类 contract kind 的通用 alias 表，而不是任务专属名词。
- 返回结构化 diagnostic，便于测试断言“为什么这条回复被拒绝”。

### `agents/office/agent.py`

- 在因澄清而暂停时，在 `needs_clarification` 中填入 `reply_contract`。
- 在恢复时，优先消费 `clarification_resolution`。
- 在灰度期间，把通用标准化结果尽量靠近执行边界地转换成现有 office 本地执行 metadata。

### `agents/compass/agent.py`

- 停止仅依赖 `interrupt.kind` 推导语义解析。
- 保留 `_office_interrupt_kind()`，但仅用于 UI / 时间线连续性。
- 用共享 contract resolver 替换当前临时拼接的 `_resolve_office_resume_reply()` 分支逻辑。
- 重问时保留同一份 `reply_contract`，只更新面向用户的消息文本。

### `agents/compass/tools.py`

- 当把 Office dispatch 结果翻译成 Compass 任务 metadata 时，保留 `reply_contract`，避免在首次暂停到后续多轮 resume 之间丢失语义信息。

## 错误处理

系统必须显式区分以下情况：

- `unknown_reply`
  文本不符合允许的契约形状。
- `ambiguous_reply`
  文本可能映射到多个结果，而契约规定 `ambiguity_policy = "reask"`。
- `missing_required_note`
  动作已识别，但缺少必需的补充自由文本。
- `stale_contract`
  任务 metadata 中的契约缺失或损坏；此时应当作为内部错误失败，而不是猜测。

所有面向用户的重试都必须让任务保持在 `INPUT_REQUIRED`，保留同一个等待中的 Office session，并回显一个明确可执行的合法回复格式。

## 测试策略

### 共享契约单元测试

- `select_option` 能解析规范 id 及支持的 alias。
- `approve_or_modify` 能解析纯动作回复和带补充说明的回复。
- 非法回复返回 `ok = False`，并附带稳定的 diagnostic reason。
- 有歧义的回复不会擅自落分支，而是触发重问。

### Compass/Office 往返测试

- 同一个 UI `interrupt.kind` 下经历两个不同 `reply_contract.kind` 阶段时，系统保持同一条等待 row，但能正确切换解析语义。
- 非法回复会保留同一个 Office session，并带着原契约重问。
- 有效回复会把标准化后的澄清结果转发给同一个 Office 任务，而不是重新启动新的 Office agent。

### 回归测试

- 现有 output-mode 和 built-in dimension 流程继续正常工作。
- custom-plan approval 不再依赖 dimension parser fallback。
- 第二次及后续的用户回复不会丢失 contract state。

## 灰度与落地顺序

为了降低副作用：

1. 本轮先保留现有 `interrupt.kind`，只用于 UI 连续性。
2. 先把 `reply_contract` 并行加到当前 metadata 中。
3. 再切换 Compass 的 resume 校验到共享 resolver。
4. 再切换 Office 的 resume 处理到标准化后的澄清结果。
5. 只有在测试覆盖共享 contract 路径后，才删除旧的临时解析分支。

## 验收标准

- 每个 Office clarification payload 都显式携带 `reply_contract`。
- Compass 的 resume 校验不再依赖被重载的 `interrupt.kind` 语义。
- Office 和 Compass 在澄清语义上使用同一条共享 resolver 路径。
- 非法回复只会重问，不会启动新的 Office session。
- 设计保持领域中立，不把任务特定业务词汇引入运行时逻辑。
