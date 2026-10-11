targetScope = 'resourceGroup'

metadata description = 'Tally — Azure Container Apps Infrastructure Deployment (Bicep)'

// ==============================================================================
// Parameters
// ==============================================================================

@description('Azure region for deployment')
param location string = resourceGroup().location

@description('Name of the Azure Container Registry (must be lowercase alphanumeric, 5-50 chars)')
param acrName string = 'tallyasd21'

@description('Name of the Azure Container Apps Managed Environment')
param environmentName string = 'cae-tally'

@description('Prefix for the Storage Account. Leave storageAccountName empty to auto-generate using this prefix.')
param storageAccountPrefix string = 'sttally'

@description('Optional explicit Storage Account name (must be globally unique, 3-24 lowercase alphanumeric chars)')
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

@description('Container image for Ollama (pulls raw image directly from Docker Hub; no custom Dockerfile needed)')
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
// 1. Base Infrastructure: Container Registry, Log Analytics, Storage
// ==============================================================================

// Deterministic unique storage account name if not explicitly provided
var actualStorageAccountName = !empty(storageAccountName) ? storageAccountName : take('${storageAccountPrefix}${uniqueString(resourceGroup().id)}', 24)

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: toLower(acrName)
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    adminUserEnabled: true
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: actualStorageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

// Replaces docker-compose volume: ollama_data -> /root/.ollama
resource ollamaShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'ollama-data'
  properties: {
    shareQuota: 100 // 100 GB for LLM model weights
  }
}

// Replaces docker-compose volumes: transactions_data, bills_data, anomalies_data, etc.
resource dbShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'db-data'
  properties: {
    shareQuota: 10
  }
}

// ==============================================================================
// 2. Azure Container Apps Managed Environment & Persistent Storage Mounts
// ==============================================================================

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: 'ollama-profile'
        workloadProfileType: ollamaWorkloadProfileType
        minimumCount: 0
        maximumCount: 2
      }
    ]
  }
}

resource ollamaStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'ollama-storage'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: ollamaShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource dbStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'db-storage'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: dbShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// Common ACR variables
var acrLoginServer = acr.properties.loginServer
var acrAdminUsername = acr.listCredentials().username
var acrAdminPassword = acr.listCredentials().passwords[0].value

// ==============================================================================
// 3. Container Modules Deployment
// Provisioning order follows strict dependency graph:
// Storage -> Databases & Ollama -> Backends -> Frontends -> Gateway
// ==============================================================================

// --- Ollama Engine ---
module modOllama 'modules/container_ollama.bicep' = {
  name: 'deploy-ollama'
  params: {
    location: location
    environmentId: environment.id
    workloadProfileName: 'ollama-profile'
    ollamaStorageName: ollamaStorage.name
    ollamaCpu: ollamaCpu
    ollamaMemory: ollamaMemory
    ollamaMinReplicas: ollamaMinReplicas
    ollamaImage: ollamaImage
    ollamaModels: ollamaModels
  }
}

// --- Databases ---
module modTransactionsDb 'modules/container_transactions_db.bicep' = {
  name: 'deploy-transactions-db'
  params: {
    location: location
    environmentId: environment.id
    dbStorageName: dbStorage.name
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
}

module modBillsDb 'modules/container_bills_db.bicep' = {
  name: 'deploy-bills-db'
  params: {
    location: location
    environmentId: environment.id
    dbStorageName: dbStorage.name
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
}

module modAnomaliesDb 'modules/container_anomalies_db.bicep' = {
  name: 'deploy-anomalies-db'
  params: {
    location: location
    environmentId: environment.id
    dbStorageName: dbStorage.name
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modTransactionsDb
  ]
}

module modBudgetsDb 'modules/container_budgets_db.bicep' = {
  name: 'deploy-budgets-db'
  params: {
    location: location
    environmentId: environment.id
    dbStorageName: dbStorage.name
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
}

module modSavingsDb 'modules/container_savings_db.bicep' = {
  name: 'deploy-savings-db'
  params: {
    location: location
    environmentId: environment.id
    dbStorageName: dbStorage.name
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
}

// --- Backends ---
module modTransactionsBackend 'modules/container_transactions_backend.bicep' = {
  name: 'deploy-transactions-backend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    enableMcp: enableMcp
    enableRag: enableRag
  }
  dependsOn: [
    modTransactionsDb
    modOllama
  ]
}

module modBillsBackend 'modules/container_bills_backend.bicep' = {
  name: 'deploy-bills-backend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    enableMcp: enableMcp
    enableRag: enableRag
  }
  dependsOn: [
    modBillsDb
    modOllama
  ]
}

module modAnomaliesBackend 'modules/container_anomalies_backend.bicep' = {
  name: 'deploy-anomalies-backend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    enableMcp: enableMcp
    enableRag: enableRag
  }
  dependsOn: [
    modAnomaliesDb
    modOllama
  ]
}

module modBudgetsBackend 'modules/container_budgets_backend.bicep' = {
  name: 'deploy-budgets-backend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    enableMcp: enableMcp
    enableRag: enableRag
  }
  dependsOn: [
    modBudgetsDb
    modOllama
  ]
}

module modSavingsBackend 'modules/container_savings_backend.bicep' = {
  name: 'deploy-savings-backend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
    enableMcp: enableMcp
    enableRag: enableRag
  }
  dependsOn: [
    modSavingsDb
    modTransactionsDb
    modOllama
  ]
}

// --- Frontends ---
module modTransactionsFrontend 'modules/container_transactions_frontend.bicep' = {
  name: 'deploy-transactions-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modTransactionsBackend
  ]
}

module modBillsFrontend 'modules/container_bills_frontend.bicep' = {
  name: 'deploy-bills-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modBillsBackend
  ]
}

module modAnomaliesFrontend 'modules/container_anomalies_frontend.bicep' = {
  name: 'deploy-anomalies-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modAnomaliesBackend
  ]
}

module modBudgetsFrontend 'modules/container_budgets_frontend.bicep' = {
  name: 'deploy-budgets-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modBudgetsBackend
  ]
}

module modSavingsFrontend 'modules/container_savings_frontend.bicep' = {
  name: 'deploy-savings-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modSavingsBackend
    modOllama
  ]
}

// --- Shared Gateway Reverse Proxy ---
module modSharedFrontend 'modules/container_shared_frontend.bicep' = {
  name: 'deploy-shared-frontend'
  params: {
    location: location
    environmentId: environment.id
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    minReplicas: minReplicas
    defaultCpu: defaultCpu
    defaultMemory: defaultMemory
  }
  dependsOn: [
    modTransactionsFrontend
    modBillsFrontend
    modAnomaliesFrontend
    modBudgetsFrontend
    modSavingsFrontend
  ]
}

// ==============================================================================
// 4. Outputs
// ==============================================================================

@description('The login server for the Azure Container Registry')
output acrLoginServer string = acr.properties.loginServer

@description('Public URL to access the Tally shared frontend (stable FQDN)')
output applicationUrl string = 'https://${modSharedFrontend.outputs.fqdn}'

@description('Public URL targeting the latest revision of the shared frontend')
output applicationLatestRevisionUrl string = 'https://${modSharedFrontend.outputs.latestRevisionFqdn}'

@description('Default domain for the Azure Container Apps environment')
output environmentDefaultDomain string = environment.properties.defaultDomain

@description('Workload profile assigned to Ollama')
output ollamaProfile object = {
  profileType: ollamaWorkloadProfileType
  cpu: ollamaCpu
  memory: ollamaMemory
  image: ollamaImage
}
