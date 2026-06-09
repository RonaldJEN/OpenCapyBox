# Subagent Run Graph Spec

## Reference

This design follows the Claude Code subagent shape in `docs/claude-code-src`:

- `tools/AgentTool/AgentTool.tsx`: Agent tool input/output, sync/background launch, agent type/name/model metadata.
- `tools/AgentTool/runAgent.ts`: subagent sidechain transcript, independent agent id, parent context inheritance, lifecycle cleanup.
- `tools/AgentTool/forkSubagent.ts`: fork guard, inherited context, child directive and anti-recursion rules.
- `tools/shared/spawnMultiAgent.ts`: teammate identity, team membership, background task state, progress visibility.

OpenCapyBox exposes `sub_agent` as a synchronous child Agent run. The parent
Agent blocks on the child, receives the child's final result as the tool result,
and the child transcript is persisted as its own `Round` connected through
`subagent_runs`. This follows the Claude Code sidechain shape: the child has its
own lifecycle/transcript while the parent only consumes the final summarized
result.

## Goals

- Represent a parent run spawning one or more subagent runs.
- Preserve the trigger point (`tool_call_id`), agent identity (`agent_name`, `agent_type`), prompt, model, isolation and lifecycle status.
- Keep runtime subagent delegation one level deep: a child Agent cannot spawn another subagent in this cut.
- Preserve graph root resolution if existing/future code creates an edge whose parent is already a child run.
- Let Web/Admin clients query the graph for inspection and future UI rendering.
- Keep current single-worker Agent execution semantics unchanged.

## Data Model

`subagent_runs` is a directed edge table:

| Field | Meaning |
|------|---------|
| `id` | Edge id |
| `user_id` / `session_id` | Owner and session |
| `root_run_id` | Top-level run that owns this graph |
| `parent_run_id` | Run that spawned the subagent |
| `child_run_id` | Spawned run, nullable before the child round exists |
| `tool_call_id` | Parent AG-UI tool call that requested the spawn |
| `agent_name` / `agent_type` / `model_id` | Subagent identity |
| `description` / `prompt` | User-visible task summary and full task prompt |
| `isolation` / `worktree_path` | Future execution isolation facts |
| `status` | `requested`, `running`, `completed`, `failed`, `cancelled` |
| `output` / `error` | Final child result or failure |
| `metadata_json` | Adapter/executor-specific details |

`child_run_id` is unique when present. Current runtime child Agents exclude
`SubAgentTool`, so automatic delegation is one level deep and child runs are
leaves in normal chat execution. The graph service still resolves
`root_run_id` through an existing parent edge when `parent_run_id` is already a
child run; this keeps manual tests and future nested graph support from losing
the top-level root.

## API

`GET /api/chat/{session_id}/round/{round_id}/subagent-graph`

Returns:

```json
{
  "session_id": "session-1",
  "root_run_id": "root-run",
  "requested_run_id": "child-run",
  "nodes": [
    {"run_id": "root-run", "kind": "root", "status": "completed"},
    {"run_id": "child-run", "kind": "subagent", "status": "completed"}
  ],
  "edges": [
    {
      "edge_id": "edge-id",
      "root_run_id": "root-run",
      "parent_run_id": "root-run",
      "child_run_id": "child-run",
      "tool_call_id": "tc-agent-1",
      "agent_type": "research",
      "status": "completed",
      "prompt": "Read docs and summarize"
    }
  ]
}
```

The endpoint accepts the root run id or any run already connected to the graph
and returns all edges sharing the resolved `root_run_id`. In the current runtime
that graph is normally one level deep.

## `sub_agent` Tool Contract

The public `sub_agent` tool is available to normal chat Agents through
`tool_factory.create_agent_tools()`. The tool owns the public schema; service
code owns child Round execution and graph lifecycle because those require DB,
AG-UI persistence and parent run metadata.

| Field | Contract |
|------|----------|
| Tool name | `sub_agent` |
| Class | `SubAgentTool` |
| Required args | `prompt` |
| Optional args | `subagent_type` (default `general`), `description` (default empty) |
| Runtime context | Supplied by `Agent.run_agui`: `thread_id`, parent `run_id`, `tool_call_id`, cancel token |
| Model | Child `AgentService` uses `ModelRegistry.get_subagent_default()` |
| Child tools | Standard chat tool set, with `AskUserQuestionTool` and `SubAgentTool` excluded |
| Output | Child Round final response returned as parent tool result |
| Invalid prompt | Tool failure: `prompt is required` |

Runtime behavior:

- The parent Agent calls `sub_agent` like any other server-side tool.
- `SubAgentTool.execute()` delegates to the service-layer runner injected by
  `AgentService`; it does not call an LLM directly.
- The service creates a `subagent_runs` edge in `requested` state, creates a
  child `Round(parent_run_id=parent_run_id)`, attaches the child run to the edge,
  then consumes the child `Agent.run_agui` stream until terminal.
- If the parent LLM emits multiple `sub_agent` tool calls in one step, the
  parent Agent executes consecutive `sub_agent` calls as a bounded parallel
  batch, limited by `AGENT_SUBAGENT_MAX_PARALLEL`. Each call creates one child
  Round and one graph edge. Parent tool results are appended back to the parent
  message history in the original tool-call order.
- Child AG-UI events and LLM call snapshots are persisted under the child
  `round_id`; they are not forwarded into the parent SSE stream.
- The parent receives one `TOOL_CALL_RESULT` for display and LLM context. The
  durable source of parent/child metadata is the `subagent_runs` edge, not the
  human-readable result text.
- UI/Admin code should resolve child run id, edge id, status, agent type, model
  id and profile from structured metadata or `subagent_runs`. Parsing
  `child_run_id:` / `edge_id:` lines from `TOOL_CALL_RESULT.content` is only a
  temporary compatibility fallback for the current web adapter and must not be
  treated as the primary contract.
- Parent history reconstruction skips rounds referenced by
  `subagent_runs.child_run_id`, preserving sidechain isolation. The parent sees
  the subagent result through its own tool result only.
- Admin audit APIs may still return child rounds, but must label them as
  `run_kind = subagent` and include the parent/subagent edge metadata so
  operators do not confuse sidechain work with ordinary chat rounds.
- Cron Agents exclude `SubAgentTool`; unattended scheduled jobs must not
  recursively delegate work through this tool.
- Child Agents also exclude `SubAgentTool`; subagent recursion is not allowed in
  this cut, and no runtime path may re-enable it for child runs.

## Runtime Contract

- Current Web send/resume/abort behavior remains unchanged.
- The `sub_agent` tool is registered for chat agents and uses
  `models.yaml` `subagent_default_model` to create a real child Agent run.
- Graph creation is done through `SubagentGraphService`; route code only queries.
- If `create_edge()` receives no explicit `model_id`, it resolves `models.yaml` `subagent_default_model`; if that field is omitted, the registry inherits `default_model`.
- Disabled/deleted users cannot create/query graph data.
- Deleting a user removes graph edges before deleting sessions/rounds.
- Subagent service execution calls:
  - `create_edge()` when a subagent is requested.
  - `attach_child_run()` when the child Round is created.
  - `mark_status()` as the child transitions or completes.

## Non-Goals For This Cut

- No real external channel teammates.
- No tmux/iTerm2/in-process teammate runner.
- No forked context prompt-cache optimization.
- No unified lanes scheduler.
- No background notification UI.
