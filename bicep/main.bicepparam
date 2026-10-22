using 'main.bicep'

param location = 'australiaeast'
// Required cloud baseline: AI mode, MCP, RAG and the Multi-Agent Server are all
// off here. main.bicep defaults enableMcp/enableRag/enableMultiAgent to true so
// the AI topology is one flag away, so the baseline must state all four.
param enableAi = false
param enableMcp = false
param enableRag = false
param enableMultiAgent = false

param deployOllamaVm = false
param ollamaVmSize = 'Standard_B4as_v2'
param adminPublicKey = ''

param multiAgentToken = readEnvironmentVariable('MULTI_AGENT_TOKEN')

// Bearer token bills-db requires on every route but /health.
param billsDbToken = readEnvironmentVariable('BILLS_DB_TOKEN')

// Stays empty on purpose. Tested 22 Oct 2026 against bills-db: an allowlist of
// the AI VM's public IP also blocks backend-to-db calls inside the environment
// ("Access denied by IP access control policy"), because inter-app traffic
// reaches the same Envoy ingress and does not arrive from that IP. These three
// DBs need app-layer bearer auth instead, which bills-db already has via
// BILLS_DB_TOKEN. Only populate this for an app with no in-environment callers.
param dbAllowedSourceIps = []
