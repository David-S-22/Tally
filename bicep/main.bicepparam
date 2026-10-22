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
