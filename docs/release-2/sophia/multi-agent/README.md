# Bills on the Multi-Agent Server — run evidence

Three real runs against the shared server (`ai-services/multi-agent-server/`, contract 1.0) with local Ollama: planner and worker `qwen2.5:3b`, reviewer `llama3.1:8b`. Each folder holds the server's own export (`workflow_runner.py --export <id> --output-dir <folder>`): `workflow_history.jsonl` (every stage snapshot, task and evidence included) and `coordination_audit.jsonl` (the ordered events). Together the three cover both surfaces, UI and terminal, and all three human decisions.

| Folder | Surface | Workflow | Agents (planner / worker / reviewer) | Reviewer | Human decision |
|---|---|---|---|---|---|
| `dispute-letter-ui/` | Bills UI, dispute panel ("Draft with agents") | `1f5b42fd…` | 8.2 / 2.2 / 23.4 s | approve_with_changes: wrong charge date, unsupported promise | **Partially Accept**: corrected letter saved as dispute draft Version 3 |
| `chat-suggestion-ui/` | Bills UI, Ask Tally ("I cancelled Spotify from 16 September") | `f27988da…` | 7.7 / 2.2 / 22.7 s | approve_with_changes: asked for fields an update doesn't need | **Accept**, against the reviewer with a reason: Spotify's end date set to 2026-09-16 |
| `terminal/` | Terminal, `workflow_runner.py` | `abeebb7f…` | 9.0 / 22.8 / 26.3 s | approve_with_changes: no cancellation date in the evidence, unrelated policy cited | **Reject**: nothing stored (`final_output: null`) |

The screenshots show the result in the UI: draft Version 3 in the dispute panel, and Spotify "Ended 16 Sep" with the suggestion resolved.

## The terminal run

`terminal/task.txt`, `evidence.json` and `context.json` are exactly what Bills sends for dispute 2 (GymCo, "Charged after I cancelled"): the task from `sophia/backend/ai/agent_tasks.py`, plus evidence from the bills database, the MCP bank comparison and the RAG billing corpus. From the repository root, with the server running:

```
python ai-services/multi-agent-server/workflow_runner.py --feature bills --task "$(cat docs/release-2/sophia/multi-agent/terminal/task.txt)" --evidence docs/release-2/sophia/multi-agent/terminal/evidence.json --context docs/release-2/sophia/multi-agent/terminal/context.json
python ai-services/multi-agent-server/workflow_runner.py --decide <workflow-id> --decision Reject --reason "The evidence has no cancellation date, so the letter cannot show the charges came after cancelling; the utility-bill payment policy does not apply to a gym membership"
python ai-services/multi-agent-server/workflow_runner.py --export <workflow-id> --output-dir <new folder>
```

Set `MULTI_AGENT_TOKEN` in the environment when the server requires one. The token is never written to these files.
