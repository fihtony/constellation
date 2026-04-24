# Multi-Agent MVP

这个 MVP 现在按 agent 目录拆分，并覆盖了你提出的几项核心要求：

1. 每个 agent 都有自己的目录：`orchestrator/`、`tracker/`、`scm/`、`android/`。
2. `orchestrator` 是常驻 Web 应用，带浏览器 UI。
3. `tracker` 和 `scm` 是常驻容器，使用各自 `.env`。
4. `android` 是按任务临时拉起的容器，启动时由 orchestrator 透传 token、LLM base URL、LLM model。
5. 制品统一由 orchestrator 写入挂载目录 `mvp/artifacts/`，但接口已经独立成 `common/artifact_store.py`，后续可平滑迁移到外部存储。
6. `tests/agent_test_targets.json` 是唯一允许的 Tracker / SCM 共享环境测试目标配置，真实写操作只能落在这里定义的 ticket / repo 上；`agent_test_targets.py` 只负责读取配置并做运行时 write guard。

## 目录结构

```text
mvp/
├── Dockerfile
├── docker-compose.yml
├── artifacts/                    # 本地挂载的 artifact 目录
├── common/                      # 共享运行时库（Registry、Launcher、LLM、ArtifactStore 等）
├── registry/
│   └── app.py                   # Capability Registry 服务
├── orchestrator/
│   ├── app.py                   # Orchestrator A2A + Web UI
│   ├── .env                     # token / Copilot Connect / artifact / launcher 配置
│   └── ui/
│       └── index.html
├── tracker/
│   ├── app.py                   # Tracker Agent（常驻）
│   └── .env
├── scm/
│   ├── app.py                   # SCM Agent（常驻）
│   └── .env
├── android/
│   ├── app.py                   # Android Agent（按任务启动）
│   └── .env
├── scripts/
│   ├── init_register.py         # 发布阶段注册所有 Agent Definition
│   ├── register_agent.py
│   └── deregister_agent.py
└── tests/
  ├── agent_test_targets.json  # 统一允许的 Tracker / SCM 真实测试目标
  ├── agent_test_targets.py    # 读取 allowlist 并做 write guard
  └── test_e2e.py
```

## 当前服务拓扑

```text
Browser / curl
    |
    v
Orchestrator :8080
    |-- query --> Registry :9000
    |-- A2A ----> Tracker Agent :8010
    |-- A2A ----> SCM Agent :8020
    |-- launch --> Android Agent container(s) on demand
    |
    +-- persist artifacts --> ./artifacts/
```

## LLM 配置

### Orchestrator `.env`

`orchestrator/.env` 里包含：

如果是新环境，优先复制 `orchestrator/.env.example`，通常只需要补齐真实凭据。

```env
OPENAI_BASE_URL=http://host.rancher-desktop.internal:1288/v1
OPENAI_MODEL=gpt-5-mini
SCM_TOKEN=replace-me
TRACKER_TOKEN=replace-me
ARTIFACT_ROOT=/app/artifacts
DOCKER_SOCKET=/var/run/docker.sock
AGENT_RUNTIME_IMAGE=mvp-agent-runtime:latest
DYNAMIC_AGENT_NETWORK=mvp-network
```

说明：

1. 这里的 `OPENAI_BASE_URL` 就是 Copilot Connect / OpenAI-compatible endpoint。
2. orchestrator 在拉起 `android` 按任务容器时，会透传 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`SCM_TOKEN`、`TRACKER_TOKEN`。
3. 如果本地没有可达的 LLM endpoint，代码默认允许回退到 mock 响应，便于离线测试。真实连通后会优先用真实 LLM。

### Long-running Agent `.env`

`tracker/.env` 默认包含：

如果是新环境，优先复制 `tracker/.env.example`，通常只需要填写 `TRACKER_TOKEN` 和 `TRACKER_EMAIL`。

```env
TRACKER_BASE_URL=https://tracker.example.com
TRACKER_API_BASE_URL=https://tracker.example.com/rest/api/3
TRACKER_CLOUD_ID=
TRACKER_TOKEN=replace-me
TRACKER_EMAIL=replace-me@example.com
TRACKER_AUTH_MODE=basic
OPENAI_BASE_URL=http://host.rancher-desktop.internal:1288/v1
OPENAI_MODEL=gpt-5-mini
```

说明：

1. 对 Tracker Cloud 的原始 API token 场景，`TRACKER_EMAIL` 和 `TRACKER_TOKEN` 都需要配置，agent 默认使用 `Basic base64(email:token)` 访问 REST API。
2. 在当前环境里，直接发送原始 `Bearer <TRACKER_TOKEN>` 到 Tracker Cloud `/rest/api/3/myself` 已验证会返回 `403 Failed to parse Connect Session Auth Token`，不要把 bearer 当作默认回退路径。
3. 对 scoped Tracker token，agent 会在站点 URL 返回 scoped-token 鉴权失败时自动切到 `https://api.atlassian.com/ex/tracker/{cloudId}/rest/api/3`；`TRACKER_CLOUD_ID` 可选，留空时会自动从 `/_edge/tenant_info` 发现。
4. 如果 `TRACKER_TOKEN` 已经是完整的 `Basic ...` 或 `Bearer ...` header 值，agent 会直接使用，不再重复包装。
5. `TRACKER_AUTH_MODE=basic` 是当前默认值；只有在明确接入非当前 Tracker Cloud 鉴权网关时才建议覆盖它。

`scm/.env` 默认包含：

如果是新环境，优先复制 `scm/.env.example`，通常只需要填写 `SCM_TOKEN`；只有在 token 需要 Basic 鉴权时才补 `SCM_USERNAME`。

```env
SCM_BASE_URL=https://scm.example.com/projects/CSM
SCM_API_BASE_URL=
SCM_TOKEN=replace-me
INSTANCE_REPORTER_ENABLED=1
OPENAI_BASE_URL=http://host.rancher-desktop.internal:1288/v1
OPENAI_MODEL=gpt-5-mini
```

设计上下文不再通过仓库内置设计服务提供，后续改由外部 MCP 接入。

## Runtime Skill 打包

`tracker/app.py` 和 `scm/app.py` 在 `process_message()` 里会直接读取 `mvp/.github/skills/.../SKILL.md`，并把正文注入各自的 LLM prompt。

这意味着：

1. 本地直接从仓库运行时，skill 文件会被正常读取。
2. 容器运行时，如果镜像里不包含 `.github/skills/`，运行时只会退化成 `No local skill guide loaded.`。
3. 当前 `tracker/Dockerfile` 与 `scm/Dockerfile` 已显式复制 `.github/skills/` 到镜像里，因此容器内也会复用同一份 skill 文本。

## 真实回归测试边界

1. Tracker 回归只允许读取和修改 `tests/agent_test_targets.json` 中定义的 `DMPP-2647`，并且所有写操作都必须恢复原状态。
2. SCM 回归只允许读取和修改 `tests/agent_test_targets.json` 中定义的 `EMF/android-test`，写入路径固定在 `agent-tests/` 下。
3. 测试脚本会在运行时显式校验 write target 是否命中 allowlist；任何不在 allowlist 中的目标都不能执行写操作。
4. 默认分支删除、整库删除、Tracker ticket 删除都不在当前 agent 能力面里，也没有公开 REST 端点。

## 外部目标解析原则

### Tracker

Tracker agent 不会再从裸 ticket key 自动构造 browse URL。更安全的做法是把完整 Tracker ticket URL 直接放到用户请求里，例如：

```text
https://tracker.example.com/browse/DMPP-2647
```

agent 会从这个显式 URL 解析 ticket key，再访问对应 API，例如：

```text
https://tracker.example.com/rest/api/3/issue/DMPP-2647
```

如果当前 token 是 scoped token，agent 仍会自动改走：

```text
https://api.atlassian.com/ex/tracker/{cloudId}/rest/api/3/issue/DMPP-2647
```

### SCM

SCM agent 也不再从内置 repo 映射里替你选择目标仓库。更安全的做法是把完整 repo browse URL 放在 Tracker ticket 或用户请求里，例如：

```text
https://scm.example.com/projects/EMF/repos/android-test/browse
```

SCM agent 会从这个显式 URL 解析 project / repo，并把解析结果写回共享工作区供下游 agent 使用。

## Artifact Store 在哪里

当前 MVP 中，artifact store 仍归 orchestrator 持有，但已经抽象成独立模块：

```text
common/artifact_store.py
```

运行时落盘位置是本地挂载目录：

```text
mvp/artifacts/
```

在容器内对应路径：

```text
/app/artifacts
```

每个任务会生成独立目录，例如：

```text
artifacts/
  task-0003/
    7f3d1a2c.json
    a18c0e44.json
  workspaces/
    task-0003-20260423-143501/
      tracker/
      scm/
      android/
```

所以当前实现满足：

1. Artifact 先保存在 orchestrator 侧。
2. 落本地文件，方便手工检查。
3. 共享工作区也落在同一个 artifact 根目录，当前用来承载 Tracker summary / issue payload、SCM repo resolution、Android plan / workflow summary。
4. 接口独立，后续迁移到 MinIO/S3/单独 artifact service 时不用改 agent 协议。

## 构建动态 Agent 镜像

动态 Agent（如 Android Agent）由 orchestrator 按需拉起，镜像不会在 `docker compose up` 时自动构建，
需要手动执行一次：

```bash
cd mvp
./build-agents.sh          # 构建所有动态 agent 镜像（当前：android）
./build-agents.sh android  # 只构建 android agent 镜像
```

构建完成后，再启动 compose 栈：

```bash
docker compose up --build -d
```

如果修改了 `android/` 目录下的代码，需要重新执行 `./build-agents.sh android` 后再测试，
因为 compose 不会自动重建动态 agent 的镜像。

## 启动方式

### 1. 启动全部常驻服务

```bash
cd mvp
docker compose up --build -d
```

这里会启动：

1. `registry`
2. `init-register`
3. `tracker`
4. `scm`
5. `orchestrator`

不会预启动 `android`，它会在任务需要时由 orchestrator 通过 Docker socket 按需拉起。

### 2. 检查服务状态

```bash
docker compose ps
```

预期：

1. `registry` healthy
2. `tracker` healthy
3. `scm` healthy
4. `orchestrator` healthy
5. `init-register` exited (0)

### 3. 验证 Registry

```bash
curl -s http://localhost:9000/agents | python3 -m json.tool
curl -s http://localhost:9000/agents/tracker-agent/instances | python3 -m json.tool
curl -s http://localhost:9000/agents/scm-agent/instances | python3 -m json.tool
curl -s http://localhost:9000/agents/android-agent/instances | python3 -m json.tool
```

预期：

1. Tracker / SCM Definition 已注册且有实例。
2. Android Definition 已注册，但初始没有实例。

## 如何和 orchestrator 通信

### 浏览器方式

打开：

```text
http://localhost:8080/
```

页面里可以：

1. 直接输入用户请求。
2. 留空 `Requested Capability`，让 orchestrator 自动规划 workflow。
3. 强制指定 `tracker.ticket.fetch` / `scm.repo.inspect` / `android.task.execute`。
4. 查询 task 和 artifacts。
5. 点击内置场景按钮做手工验证。

### curl 方式

#### 场景 1: 只走 Tracker agent

```bash
curl -s -X POST http://localhost:8080/message:send \
  -H 'Content-Type: application/json' \
  -d '{
    "requestedCapability": "tracker.ticket.fetch",
    "message": {
      "messageId": "msg-tracker-001",
      "role": "ROLE_USER",
      "parts": [{"text": "Please analyze https://tracker.example.com/browse/DMPP-2647"}]
    }
  }' | python3 -m json.tool
```

#### 场景 2: 只走 SCM agent

```bash
curl -s -X POST http://localhost:8080/message:send \
  -H 'Content-Type: application/json' \
  -d '{
    "requestedCapability": "scm.repo.inspect",
    "message": {
      "messageId": "msg-scm-001",
      "role": "ROLE_USER",
      "parts": [{"text": "Find the Android repository in SCM and summarize where to start."}]
    }
  }' | python3 -m json.tool
```

#### 场景 3: 自动多 agent workflow

```bash
curl -s -X POST http://localhost:8080/message:send \
  -H 'Content-Type: application/json' \
  -d '{
    "message": {
      "messageId": "msg-workflow-001",
      "role": "ROLE_USER",
      "parts": [{"text": "Analyze https://tracker.example.com/browse/DMPP-2647 and prepare the Android implementation plan for https://scm.example.com/projects/EMF/repos/android-test/browse."}]
    }
  }' | python3 -m json.tool
```

这个请求会让 orchestrator 自动推导 workflow：

1. `tracker.ticket.fetch`
2. `scm.repo.inspect`
3. `android.task.execute`

并在没有 Android 空闲实例时动态拉起一个 Android 容器。

#### 场景 4: 查询任务

```bash
curl -s http://localhost:8080/tasks/task-0001 | python3 -m json.tool
curl -s http://localhost:8080/tasks/task-0001/artifacts | python3 -m json.tool
```

## Browser UI 手工测试用例

建议按顺序测试：

1. Tracker Ticket
  输入：`Please analyze https://tracker.example.com/browse/DMPP-2647`
   预期：最终 routed to `tracker-agent`

2. SCM Repo
   输入：`Find the Android repository in SCM and summarize where to start.`
   预期：最终 routed to `scm-agent`

3. Android Workflow
  输入：`Analyze https://tracker.example.com/browse/DMPP-2647 and prepare the Android implementation plan for https://scm.example.com/projects/EMF/repos/android-test/browse.`
   预期：workflow 会串联 Tracker、SCM、Android，最终 state=`TASK_STATE_COMPLETED`

4. Missing Capability
   capability: `openshift.cluster.inspect`
   预期：`NO_CAPABLE_AGENT`

5. Malformed Request
   预期：HTTP 400 + `missing message`

## 端到端测试

```bash
python3 tests/test_e2e.py
python3 tests/test_e2e.py -v
```

## Tracker 定向回归测试

```bash
./venv/bin/python tests/test_tracker_agent.py -v
./venv/bin/python tests/test_tracker_agent.py --container -v
```

当前这组测试会覆盖：

1. Bearer 诊断失败与 Basic(email:token) 成功。
2. `/tracker/myself`、JQL 搜索、ticket 获取、comment CRUD、transition 列表。
3. 真实状态流转，并在同一次测试里恢复到原始状态。
4. assignee 变更，并恢复到原始 assignee。
5. 容器模式下的 Tracker agent 与 CA bundle 配置。

## SCM 定向回归测试

```bash
./venv/bin/python tests/test_scm_agent.py --agent-url http://127.0.0.1:18020 -v
```

当前这组测试会覆盖：

1. SCM Git over HTTPS token 鉴权。
2. `/scm/search/repos`、`/scm/branches`。
3. 创建分支、真实 push、创建真实 PR。
4. `/scm/pull-requests/{id}` 明细查询与 `linkedTrackerIssues` 提取。
5. `/scm/pull-requests` 列表查询与 `linkedTrackerIssues` 提取。
6. PR inline comment。

如果只是本地临时起 agent 做定向测试，建议设置：

```bash
INSTANCE_REPORTER_ENABLED=0
```

这样可以在没有本地 registry 的情况下关闭实例上报噪音，不影响 agent 对外 HTTP 接口验证。

当前测试覆盖：

1. 服务健康检查
2. Agent Card 发现
3. Registry Definition / Instance 状态
4. Tracker capability 路由
5. SCM capability 路由
6. 自动多 agent workflow
7. Android 按需拉起后自动清理
8. 任务查询与 artifact 落盘
9. 缺失 capability
10. Tracker deregister / reregister
11. busy-capacity 语义
12. direct downstream agent communication
13. orchestrator browser UI
14. malformed request handling

## 设计对齐说明

相对设计文档，这个 MVP 现在已经补齐这些点：

1. `Capability Registry` 与 `Agent Instance` 两层状态。
2. 每个 agent 一个目录。
3. long-running agent 使用各自 `.env`。
4. orchestrator 持有 token / Copilot Connect 配置并可在动态拉起 Android 时透传。
5. Android agent 从“假 agent/硬编码”升级为真实 LLM 调用能力。
6. ArtifactStore 从内存升级为本地挂载目录持久化。
7. orchestrator 具备 browser UI 和手工验证场景。

仍然保留的 MVP 边界：

1. Tracker / SCM 的字段覆盖仍然只聚焦 MVP 端点，但 Tracker Cloud Basic(email:token) 鉴权、comment CRUD、assignee 变更、可回滚状态流转已经在本地和 Tracker 容器内验证通过。

## Workspace Skills 与 Agent Capabilities 的关系

`mvp/.github/skills/` 下的 `SKILL.md` 与各 agent `agent-card.json` 里的 `skills` 不是同一层东西：

1. `mvp/.github/skills/tracker-cloud-workflow/SKILL.md`、`mvp/.github/skills/scm-server-workflow/SKILL.md` 是 GitHub Copilot workspace skills，会在用户直接和 Copilot 对话、且描述匹配时被自动加载。
2. `tracker/agent-card.json`、`scm/agent-card.json` 里的 `skills` 是 A2A capability 广告，用来告诉 orchestrator 或别的 agent 这些 HTTP agent 提供哪些机器可调用能力。
3. 当前实现里，这两层已经做了显式复用：`tracker/app.py` 和 `scm/app.py` 会在 `process_message()` 中读取对应 `SKILL.md`，再把 skill 内容注入 LLM prompt，作为本地操作指南。
4. 这意味着 Python agent runtime 不会“执行” `SKILL.md`，但会把其中的知识作为提示上下文复用。
5. 因为当前只分享 `mvp/` 这个 git repo，真正会随仓库一起共享和生效的 skill 应该放在 `mvp/.github/skills/`，而不是依赖同级未共享目录里的 skill。
2. Android agent 当前生成的是实现计划和测试建议，不直接改代码仓库。
3. ArtifactStore 仍由 orchestrator 持有，只是现在已经可单独迁移。