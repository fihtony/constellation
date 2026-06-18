# Changelog

All notable changes to Constellation are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-06-18

Version 1.1.0 strengthens Constellation's production execution model. The
release standardizes the agentic runtime contract, adds safer Copilot CLI
support, tightens permission enforcement across child agents, improves office
and development workflow reliability, and simplifies Docker/Rancher
deployment.

### Highlights

- **Unified, policy-aware agentic runtime contract.** Runtime adapters now
  advertise the `run_agentic()` features they can enforce, including
  Constellation tools, MCP servers, `cwd`, `allowed_tools`, and continuations.
  Requests that exceed a backend's supported surface fail closed before the
  agent launches, while shared result types make downstream validation
  consistent across `claude-code`, `copilot-cli`, `codex-cli`, and
  `connect-agent`.

- **Managed tool execution for CLI backends.** Copilot CLI and Codex CLI can
  run through Constellation's managed ReAct loop when they need access to
  framework tools. The model emits structured tool requests, the framework
  executes them through `ToolRegistry` under the active `PermissionEngine`,
  and every tool call remains bounded, auditable, and permission checked.

- **Copilot CLI BYOK support with host-credential isolation.** The Copilot CLI
  adapter now supports custom OpenAI, Azure, and Anthropic-compatible provider
  endpoints through `COPILOT_PROVIDER_*` and `COPILOT_MODEL` settings. The
  runtime no longer depends on a developer's host GitHub authentication, and
  host `GH_TOKEN` / `GITHUB_TOKEN` values are stripped from subprocess
  environments unless an explicit deployment credential is configured.

- **Stronger least-privilege execution boundaries.** Web Dev and Code Review
  now require `executionContract` metadata from the parent workflow, resolve
  that contract against their local permission profiles, and pass the narrowed
  tool surface into each agentic step. Command allow/deny patterns, agent
  launch permissions, structured permission-denial logs, and post-agentic
  validation records provide clearer enforcement and diagnostics.

- **More reliable development-agent handoffs.** Team Lead, Web Dev, and Code
  Review now compact Jira context and delivery plans before delegation, share
  robust JSON extraction for LLM outputs, preserve major-step progress across
  child agents, and handle backend-specific self-assessment and remediation
  responses more consistently. Repository URL parsing, Jira/SCM context
  extraction, and design-context retry handling were also hardened.

- **Improved Office workflows.** The Office agent now handles clarification
  round trips more reliably, avoids stray output during summarization, verifies
  expected deliverables more precisely, surfaces directory patterns for custom
  organization requests, and chunks custom-classification work for larger
  folders. These changes apply across report analysis, document
  summarization, and folder organization in both `workspace` and `inplace`
  delivery modes.

- **Leaner container build and startup flow.** Agent images now share
  `constellation-base:agentic-<runtime>` and `constellation-base:boundary`
  layers, reducing duplication across per-agent Dockerfiles. `start.sh`,
  `stop.sh`, `scripts/build_base.sh`, `docker-compose-v2.yml`, and the
  Rancher override were updated so Docker and Rancher deployments use the same
  base-image contract with only runtime-specific overrides.

### Added

- `framework/agentic_policy.py` for runtime-specific tool-policy mapping and
  post-step validation.
- `framework/runtime/managed_agentic.py` for Constellation-managed tool loops
  on text-oriented CLI backends.
- `framework/context_budget.py` and `framework/json_extract.py` for compact,
  backend-stable prompt context and robust structured-output parsing.
- `framework/audit_log.py` for structured permission-denial records.
- `framework/runtime/cli_prompt.py` for safe prompt handoff to CLI adapters,
  including large-prompt spooling.
- `docker/Dockerfile.base` and `scripts/build_base.sh` for shared agentic and
  boundary base images.
- Expanded runtime, permission, container, office, Web Dev, Team Lead, Code
  Review, and configuration tests.

### Changed

- `config/.env.example` now documents shared runtime selectors, boundary
  backend selectors, timezone handling, Claude Code settings, Copilot CLI
  BYOK settings, Codex CLI settings, and Connect-Agent settings in one place.
- Per-agent Dockerfiles now build on the shared base-image flavors instead of
  each installing the full dependency/runtime surface independently.
- Web Dev prompts and node logic now use compacted Jira and delivery-plan
  context to reduce prompt size and backend drift.
- Connect-Agent, Claude Code, Copilot CLI, and Codex CLI adapters now expose
  consistent agentic capability metadata and request validation.
- Team Lead and Web Dev permission profiles now define narrower launch,
  command, and tool surfaces for child-agent execution.

### Fixed

- Fixed Copilot CLI development-task failures caused by prompt handling,
  backend error reporting, self-assessment parsing, and missing managed-tool
  behavior.
- Fixed repository URL parsing edge cases in SCM handoffs.
- Fixed single-shot transport failures so `URLError` subtypes are surfaced in
  diagnostics.
- Fixed Jira context bloat across agent handoffs by compacting implementation
  details before child-agent delegation.
- Fixed Office custom-dimension organization behavior for larger folder sets
  and directory-pattern discovery.
- Fixed Team Lead Jira/SCM context extraction and design-context retry
  behavior.

### Validation

- Added broad unit coverage for the unified runtime contract, managed agentic
  loop, JSON extraction, context budgeting, permission YAML, command policy,
  tool registry behavior, Copilot CLI runtime configuration, Web Dev
  execution cycles, Office custom organization, Team Lead planning, and Code
  Review.
- Extended Office end-to-end coverage for containerized and local workflows.

## [1.0.0] - 2026-06-12

The first public release of Constellation. The system is a multi-agent platform
that takes a user request (e.g. "summarize these documents" or "implement this
Jira ticket") and executes it through a typed graph of cooperating agents, with
strict contracts for tool access, capability routing, and delivery verification.

### Highlights

- **End-to-end development workflows.** A user-supplied Jira ticket can be
  driven all the way to a merged pull request through the multi-agent pipeline
  (`compass` → `team_lead` → `web_dev` → `code_review` → `scm` → `jira`),
  rather than collapsed into a single LLM call. The agents exchange structured
  state instead of unstructured text, so each step is auditable and resumable.

- **Three office document workflows.** The `office` agent ships with three
  capabilities out of the box: **report analysis** (CSV / XLSX / PDF / Word /
  PowerPoint / plain text), **document summarization** (per-file and combined),
  and **folder organization** (size / type / time / filename / custom
  dimension). All three run on the same shared agent definition and share the
  same input validation, integrity check, and delivery contract.

- **Two output delivery modes for every office task.** Each office capability
  supports both **`in-place`** delivery (the deliverables are written alongside
  the user's source files, so the user sees a single, self-consistent tree)
  and **`workspace`** delivery (the source is read-only and deliverables land
  in the office artifacts directory, ideal for sandboxed or shared inputs).
  The user picks the mode at task submission time; the prompt, executor, and
  verifier all derive their paths from the same helper, so the two modes can
  never disagree about where a file is supposed to land.

- **Pluggable boundary-agent integrations.** Constellation ships first-class
  boundary agents for the external systems an enterprise team actually uses:
  - **Jira** — REST API and MCP transports
  - **GitHub** — REST API and MCP transports
  - **Bitbucket** — REST API
  - **Stitch** — MCP transport (for UI design)
  - **Figma** — REST API (for UI design)  
  Each boundary agent is registered through the Capability Registry rather
  than wired by URL, so swapping the transport (e.g. moving Jira from REST to
  MCP) is a configuration change rather than a code change.

- **Dual-container deployment story.** The same agent images run on plain
  Docker Compose and on Rancher-managed container orchestration. Networking,
  DNS, storage mounts, and custom-CA-certificate loading are validated for
  both, so the deployment surface is consistent whether the team uses
  upstream Docker or a Rancher-managed cluster.

- **Claude Code CLI as the agentic LLM runtime.** The runtime is fronted by a
  unified `AgentRuntimeAdapter` contract. The Claude Code CLI backend
  (`claude --print` for single-shot, `claude --print` over MCP for agentic
  ReAct) is the production default; the Connect-Agent, Codex CLI, and
  Copilot CLI backends are first-class peers so additional providers can be
  added without touching any call site.

### Validation

Every feature above is covered by the unit, integration, and end-to-end test
suites shipped with this release:

- The unit suite exercises the framework, every agent, every boundary
  integration, and the office document workflows (including both output modes
  and both deployment topologies).
- The integration suite validates live traffic against the boundary agents.
- The end-to-end suite drives full multi-agent chains through Compass, Team
  Lead, Office, and the development workflow.

### Upgrade notes

- This is the first tagged release, so there is no upgrade path from a prior
  version. Install the tag and follow the quick-start in `README.md`.
- `pyproject.toml` still carries the pre-tag internal version; it will be
  aligned with the git tag in a subsequent release.

[1.1.0]: https://github.com/fihtony/constellation/releases/tag/v1.1.0
[1.0.0]: https://github.com/fihtony/constellation/releases/tag/v1.0.0
