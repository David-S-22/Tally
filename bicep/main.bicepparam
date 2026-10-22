using 'main.bicep'

param location = 'australiaeast'
// Required cloud baseline: AI mode, MCP, RAG and the Multi-Agent Server are all
// off. Multi-Agent has no Bicep parameter; its backends default it off in code.
param enableAi = false
param enableMcp = false
param enableRag = false

param deployOllamaVm = false
param ollamaVmSize = 'Standard_B4as_v2'
param adminPublicKey = ''
