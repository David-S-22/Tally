# Bills — Release 2 design flow

Owner: Sophia Nguyen (Bills List & Dispute Assistant). Branch `sophia/r2-bills-multi-agent`, PR #220.

The Release 1 flows are unchanged: the Bills backend reaches the shared MCP server for bank facts and the shared RAG server (through the MCP tool `retrieve_context`) for policy facts, and Ollama for the single-model Ask Tally reply and dispute letter. Release 2 adds the shared Multi-Agent Server as a fourth non-containerised local service. The backend gathers evidence through the Release 1 clients, sends one task to the server, the three agents run in sequence, and the human decides in the Bills UI before anything is written.

## 1. Release 2 flow: dispute letter through the Multi-Agent Server

```mermaid
sequenceDiagram
    autonumber
    participant UI as Bills UI<br/>dispute panel
    participant BE as bills-backend<br/>:5005
    participant MCP as MCP server<br/>:8000
    participant MA as Multi-Agent<br/>Server :5010
    participant P as Planner<br/>qwen2.5:3b
    participant W as Worker<br/>qwen2.5:3b
    participant R as Reviewer<br/>llama3.1:8b

    UI->>BE: "Draft with agents"
    BE->>MCP: compare_bill_with_bank_charges
    MCP-->>BE: bank charges, 90 days
    BE-->>UI: 202 + polling block (2 s)
    BE->>MA: POST /workflow<br/>task, evidence[], context
    MA->>P: task + evidence
    P-->>MA: plan, missing_information
    MA->>W: task + plan + evidence
    W-->>MA: letter, evidence_used
    MA->>R: plan + evidence + letter
    R-->>MA: verdict, risks
    Note over MA: record + audit events saved
    MA-->>BE: record, decision pending
    BE-->>UI: plan · editable letter · risks<br/>Accept / Accept with edits / Reject
    Note over UI: human decides (reason if editing<br/>or overriding the reviewer)
    UI->>BE: decision
    BE->>MA: POST /workflow/{id}/decision
    MA-->>BE: final_output recorded
    Note over BE: Accept or edit → new dispute draft<br/>in bills-db · Reject → nothing written
    BE-->>UI: dispute panel, new version
```

The Ask Tally suggestion workflow is the same sequence with a different task: the backend sends the user's words and the bill rows as evidence, the Worker returns the change as a JSON object (`{op, entity, id, fields}`), and Accept applies it through the Release 1 suggestions flow after the same vetting every chat proposal gets. Plain questions never reach the server.

## 2. Bills architecture with the Release 2 service

```mermaid
flowchart TB
    subgraph compose["Docker Compose · containerised"]
        FE["bills-frontend<br/>nginx + HTMX · :3005"] -->|"/bills-backend/*"| BE["bills-backend<br/>Flask · :5005"]
        BE --> DB[("bills-db · :6005")]
    end

    subgraph host["Host processes · non-containerised · reached via host.docker.internal"]
        MCPS["MCP server · :8000 (R1)<br/>bills + transactions tools"]
        RAG["RAG server · :5003 (R1)<br/>retrieval only"]
        MA["Multi-Agent Server · :5010 (R2)<br/>Planner → Worker → Reviewer"]
        OL["Ollama · :11434<br/>AI-Mode (R0)<br/>AI_MODE_ENABLED"]
        MCPS -->|"retrieve_context"| RAG
        MA -->|"3 sequential calls"| OL
    end

    BE -->|"compare_bill_with_bank_charges<br/>retrieve_context"| MCPS
    BE -->|"POST /workflow<br/>POST /workflow/id/decision<br/>bearer token"| MA
    BE -->|"chat reply, single-model letter"| OL

    classDef r2 stroke:#2f7df6,stroke-width:2.5px;
    class MA r2;
```

Switches: `AI_MODE_ENABLED`, `MCP_ENABLED`, `RAG_ENABLED`, `MULTI_AGENT_ENABLED` on bills-backend (code default off; CI and the cloud baseline `false`; compose defaults them on for the local AI topology). With a switch off the matching route answers 503 with a safe code (`ai_disabled`, `multi_agent_disabled`) and the rest of Bills works unchanged. `AI_MODE_ENABLED` gates `guard.run` before its first attempt, so a disabled deployment refuses rather than serving the templated fallback as though the model had replied, and `/health` reports `modes.ai: "disabled"` without probing Ollama.

## 3. Deployment flow (Bills slice of the group pipeline)

```mermaid
flowchart LR
    DEV["Developer commit<br/>(pre-commit: Ruff security, detect-secrets, pip-audit — group hooks)"]
    PR["Pull request to main"]
    CI["Sophia-CI.yml<br/>test (178 core tests) ·<br/>agentic-loop (41) ·<br/>docker-health (bills containers, modes off)"]
    MAIN["Merge to main"]
    CD["cloud-deployment.yml<br/>(David) build → ACR → Bicep<br/>./deploy.sh containers (default)"]
    ACA["Azure Container Apps<br/>bills-frontend / backend / db<br/>AI-Mode, MCP, RAG, Multi-Agent off<br/>enableAi / enableMcp / enableRag = false"]
    DEV --> PR --> CI --> MAIN --> CD --> ACA
```

`./deploy.sh` defaults to `containers`, the required baseline: no Ollama VM, and all four switches off. `./deploy.sh all` provisions the Ollama VM for the optional AI topology (Bonus Marks), and AI mode still needs `enableAi=true` passed explicitly — the VM existing does not turn AI on. `cloud-deployment.yml` passes `containers` or `update` explicitly, so the pipeline does not deploy the VM; the old `all` default only applied to a hand-run `./deploy.sh`.

The two post-commit endpoint tests Sophia-CI runs on every push (criterion 4) are `test_multi_agent_routes.py::test_letter_workflow_sends_bills_evidence_and_waits_for_the_human` (`POST /api/disputes/<id>/workflow`) and `::test_accept_stores_the_letter_as_the_next_draft_with_the_review` (`POST /api/disputes/<id>/workflow/<workflow_id>/decision`), both driving the real server coordinator with a fake model on the live seeded database; the `docker-health` job then calls the built container's `/health`, `/api/evidence` and `/api/tools/retrieve_context` and asserts the modes-off refusals.
