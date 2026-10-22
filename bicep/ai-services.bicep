targetScope = 'resourceGroup'
param location string = 'australiaeast'
@minLength(1)
param adminPublicKey string
@description('Shared host for MCP, RAG and multi-agent: 2 vCPU, 8 GiB RAM')
param vmSize string = 'Standard_B2as_v2'
@description('Daily shutdown in local Sydney time (HHmm)')
param autoShutdownTime string = '0000'
param autoShutdownTimeZone string = 'AUS Eastern Standard Time'
param containerDomain string
@secure()
@minLength(32)
param multiAgentToken string
var services = [
  { name: 'mcp', port: 8000 }
  { name: 'rag', port: 5003 }
  { name: 'multi-agent', port: 5010 }
]
module host 'modules/python-service-vm.bicep' = {
  name: 'deploy-ai-vm'
  params: {
    location: location
    vmName: 'vm-tally-ai'
    dnsLabel: 'tally-ai'
    services: services
    serviceEnvironment: join([
      'OLLAMA_URL=http://tally-ollama.${location}.cloudapp.azure.com:11434'
      'TRANSACTIONS_DB_URL=https://transactions-db.${containerDomain}'
      'ANOMALIES_DB_URL=https://anomalies-db.${containerDomain}/anomalies'
      'BILLS_DB_URL=https://bills-db.${containerDomain}'
      'RAG_SERVER_URL=http://127.0.0.1:5003'
      'MULTI_AGENT_HOST=0.0.0.0'
      'MULTI_AGENT_TOKEN=${multiAgentToken}'
      'MULTI_AGENT_LOG_DIR=/var/lib/tally/logs'
      'AGENT_STEP_TIMEOUT_SECONDS=600'
      'MULTI_AGENT_WORKFLOW_TIMEOUT_SECONDS=1800'
    ], '\n')
    adminPublicKey: adminPublicKey
    vmSize: vmSize
    autoShutdownTime: autoShutdownTime
    autoShutdownTimeZone: autoShutdownTimeZone
  }
}
output endpoints array = [for service in services: {
  name: service.name
  url: 'http://${host.outputs.fqdn}:${service.port}'
}]
