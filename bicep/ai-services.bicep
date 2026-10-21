targetScope = 'resourceGroup'
param location string = 'australiaeast'
@minLength(1)
param adminPublicKey string
@description('Small burstable hosts for lightweight HTTP services')
param lightweightVmSize string = 'Standard_B2ats_v2'
@description('Extra memory for Chroma and local embeddings')
param ragVmSize string = 'Standard_B2als_v2'
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
module hosts 'modules/python-service-vm.bicep' = [for service in services: {
  name: 'deploy-${service.name}-vm'
  params: {
    location: location
    vmName: 'vm-tally-${service.name}'
    dnsLabel: 'tally-${service.name}'
    servicePort: service.port
    serviceName: service.name
    serviceEnvironment: join([
      'OLLAMA_URL=http://tally-ollama.${location}.cloudapp.azure.com:11434'
      'TRANSACTIONS_DB_URL=https://transactions-db.${containerDomain}'
      'ANOMALIES_DB_URL=https://anomalies-db.${containerDomain}/anomalies'
      'BILLS_DB_URL=https://bills-db.${containerDomain}'
      'RAG_SERVER_URL=http://tally-rag.${location}.cloudapp.azure.com:5003'
      'MULTI_AGENT_HOST=0.0.0.0'
      'MULTI_AGENT_TOKEN=${multiAgentToken}'
      'MULTI_AGENT_LOG_DIR=/var/lib/tally/logs'
    ], '\n')
    adminPublicKey: adminPublicKey
    vmSize: service.name == 'rag' ? ragVmSize : lightweightVmSize
    autoShutdownTime: autoShutdownTime
    autoShutdownTimeZone: autoShutdownTimeZone
  }
}]
output endpoints array = [for (service, i) in services: {
  name: service.name
  url: 'http://${hosts[i].outputs.fqdn}:${service.port}'
}]
