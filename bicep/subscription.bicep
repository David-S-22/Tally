targetScope = 'subscription'

metadata description = 'Tally — Azure Subscription-Level Deployment with Resource Group Creation'

// ==============================================================================
// Parameters
// ==============================================================================

@description('Name of the Azure Resource Group')
param resourceGroupName string = 'ASD-GROUP-21'

@description('Azure region for deployment')
param location string = 'australiaeast'

@description('Name of the Azure Container Registry (must be lowercase alphanumeric, 5-50 chars)')
param acrName string = 'tallyasd21'

@description('Name of the Azure Container Apps Managed Environment')
param environmentName string = 'cae-tally'

@description('Prefix for the Storage Account')
param storageAccountPrefix string = 'sttally'

@description('Optional explicit Storage Account name')
param storageAccountName string = ''

@description('Minimum number of replicas for container apps (0 enables scale-to-zero when idle)')
param minReplicas int = 0

@description('Workload profile compute SKU for Ollama (e.g. D4, D8, D16)')
param ollamaWorkloadProfileType string = 'D8'

@description('vCPU allocated to Ollama')
param ollamaCpu string = '4.0'

@description('Memory allocated to Ollama')
param ollamaMemory string = '16Gi'

@description('Minimum replicas for Ollama (0 enables scale-to-zero when idle)')
param ollamaMinReplicas int = 0

@description('Container image for Ollama')
param ollamaImage string = 'ollama/ollama:latest'

@description('Models for Ollama to pull on startup')
param ollamaModels string = 'qwen2.5:0.5b llama3.1:8b qwen2.5:3b qwen3:4b'

@description('vCPU allocated to each standard microservice container')
param defaultCpu string = '2.0'

@description('Memory allocated to each standard microservice container')
param defaultMemory string = '4Gi'

@description('Image tag to deploy for application containers')
param imageTag string = 'latest'

@description('Enable Model Context Protocol (MCP) integrations in backend services')
param enableMcp bool = false

@description('Enable Retrieval-Augmented Generation (RAG) integrations in backend services')
param enableRag bool = false

// ==============================================================================
// Resource Group & Main Infrastructure Deployment
// ==============================================================================

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

module main 'main.bicep' = {
  name: 'tally-main-deployment'
  scope: rg
  params: {
    location: location
    acrName: acrName
    environmentName: environmentName
    storageAccountPrefix: storageAccountPrefix
    storageAccountName: storageAccountName
    minReplicas: minReplicas
    ollamaWorkloadProfileType: ollamaWorkloadProfileType
    ollamaCpu: ollamaCpu
    ollamaMemory: ollamaMemory
    ollamaMinReplicas: ollamaMinReplicas
    ollamaImage: ollamaImage
    ollamaModels: ollamaModels
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    imageTag: imageTag
    enableMcp: enableMcp
    enableRag: enableRag
  }
}

// ==============================================================================
// Outputs
// ==============================================================================

@description('The login server for the Azure Container Registry')
output acrLoginServer string = main.outputs.acrLoginServer

@description('Public URL to access the Tally shared frontend (stable FQDN)')
output applicationUrl string = main.outputs.applicationUrl

@description('Public URL targeting the latest revision of the shared frontend')
output applicationLatestRevisionUrl string = main.outputs.applicationLatestRevisionUrl

@description('Default domain for the Azure Container Apps environment')
output environmentDefaultDomain string = main.outputs.environmentDefaultDomain

@description('Workload profile assigned to Ollama')
output ollamaProfile object = main.outputs.ollamaProfile
