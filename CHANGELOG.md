# Changelog

All notable changes to Constellation are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/fihtony/constellation/releases/tag/v1.0.0
