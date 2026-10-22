# Anomalies: Multi-Agent Server integration

The Anomalies backend can route transaction reviews through the shared
Planner–Worker–Reviewer service while retaining the feature's existing
transaction queue, MCP examples, RAG retrieval, anomaly database, and human
review UI.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MULTI_AGENT_ENABLED` | `false` outside Compose | Selects the shared workflow instead of direct Ollama review. |
| `MULTI_AGENT_SERVER_URL` | `http://host.docker.internal:5010` | Shared server origin. |
| `MULTI_AGENT_TOKEN` | Empty | Optional bearer token, sent only from the backend. |
| `MULTI_AGENT_TIMEOUT_SECONDS` | `600` | Must exceed the server's default 540-second workflow deadline. |

Compose enables the integration by default and allows the URL, token, and
timeout to be overridden by `MULTI_AGENT_URL`, `MULTI_AGENT_TOKEN`, and
`MULTI_AGENT_TIMEOUT`. The shared service remains a separately started host
process.

## Review and decision flow

The background worker obtains the current transaction, previous confirmed and
dismissed examples, and available RAG guidance through existing Anomalies
clients. It submits those as evidence under feature `anomalies`, asks the Worker
for structured `is_suspicious` and `justification` fields, and validates the
response before storing a finding. The anomaly record retains the workflow ID
and the RAG source filenames. Non-suspicious results do not create anomaly
records.

Workflow creation is not blindly retried after a timeout. The backend queries
the latest Anomalies workflow and adopts it only when its evidence contains the
unique source identifier for the review request. If no matching saved workflow can
be established, the transaction review fails visibly in backend logs rather
than creating a duplicate.

The review dialog requires a reason for findings linked to a shared workflow.
Confirm maps to `Accept`; dismiss maps to `Reject`. The backend records the
decision with the shared service before updating local status. If the request
times out, it checks the saved workflow for the exact same decision and reason
before considering the decision complete. Service errors leave the local
anomaly unreviewed, and the UI keeps the dialog open to allow retry.

Existing records remain compatible: `workflow_id` is nullable and existing
anomalies without one continue to use the local confirm/dismiss behavior.
