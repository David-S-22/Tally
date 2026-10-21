targetScope = 'resourceGroup'

metadata description = 'Tally — Azure Container Apps Infrastructure & Microservices Deployment'

// ==============================================================================
// Parameters
// ==============================================================================

@description('Azure region for deployment')
param location string = resourceGroup().location

@description('Azure region for the Azure Container Registry')
param acrLocation string = 'australiaeast'

@description('Name of the Azure Container Registry (must be lowercase alphanumeric, 5-50 chars)')
param acrName string = 'tallyasd21'

@description('Name of the Azure Container Apps Managed Environment')
param environmentName string = 'cae-tally'

@description('Minimum number of replicas for container apps (0 enables scale-to-zero when idle)')
param minReplicas int = 0

@description('Whether to provision the dedicated Ollama VM along with the container apps')
param deployOllamaVm bool = true

@description('VM Compute SKU for Ollama (Standard_B4as_v2: 4 vCPU, 16 GiB RAM)')
param ollamaVmSize string = 'Standard_B4as_v2'

@description('SSH RSA Public Key for Ollama VM access')
param adminPublicKey string = ''

@description('DNS label for Ollama VM public IP')
param ollamaDnsLabel string = 'tally-ollama'

@description('Base URL for the external Ollama VM API')
param ollamaUrl string = 'http://${ollamaDnsLabel}.${location}.cloudapp.azure.com:11434'

@description('Daily auto-shutdown time in 24-hour format (0000 = 12:00 AM midnight AEST)')
param autoShutdownTime string = '0000'

@description('vCPU allocated to lightweight microservice containers (databases, frontends)')
param defaultCpu string = '0.25'

@description('Memory allocated to lightweight microservice containers (databases, frontends)')
param defaultMemory string = '0.5Gi'

@description('vCPU allocated to backend API microservice containers')
param backendCpu string = '0.5'

@description('Memory allocated to backend API microservice containers')
param backendMemory string = '1.0Gi'

@description('Image tag to deploy for application containers')
param imageTag string = 'latest'

@description('Enable Model Context Protocol (MCP) integrations in backend services')
param enableMcp bool = false

@description('Enable Retrieval-Augmented Generation (RAG) integrations in backend services')
param enableRag bool = false

// ==============================================================================
// 1. Base Infrastructure: Container Registry & Log Analytics
// ==============================================================================

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: acrLocation
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

// ==============================================================================
// 2. Azure Container Apps Managed Environment
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
    ]
  }
}

// Common ACR variables
var acrLoginServer = acr.properties.loginServer
var acrAdminUsername = acr.listCredentials().username
var acrAdminPassword = acr.listCredentials().passwords[0].value
var envDomain = environment.properties.defaultDomain

// ==============================================================================
// 3. Dedicated Ollama Host VM (Conditional Provisioning)
// ==============================================================================

module modOllamaVm 'modules/ollama-vm.bicep' = if (deployOllamaVm) {
  name: 'deploy-ollama-vm'
  params: {
    location: location
    vmSize: ollamaVmSize
    adminPublicKey: adminPublicKey
    dnsLabel: ollamaDnsLabel
    autoShutdownTime: autoShutdownTime
  }
}

// ==============================================================================
// 4. Container Deployments using Reusable Modules
// ==============================================================================

// --- Databases (Reusable SQLite Module) ---
module modTransactionsDb 'modules/container-app.bicep' = {
  name: 'deploy-transactions-db'
  params: {
    name: 'transactions-db'
    location: location
    environmentId: environment.id
    targetPort: 6001
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '6001' }
      { name: 'DB_PATH', value: '/app/data/transactions.db' }
      { name: 'ANOMALIES_DB_URL', value: 'https://anomalies-db.${envDomain}/anomalies' }
      { name: 'ANOMALIES_TIMEOUT_SECONDS', value: '10' }
      { name: 'SAVINGS_DB_URL', value: 'https://savings-db.${envDomain}' }
    ]
  }
}

module modBillsDb 'modules/container-app.bicep' = {
  name: 'deploy-bills-db'
  params: {
    name: 'bills-db'
    location: location
    environmentId: environment.id
    targetPort: 6005
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '6005' }
      { name: 'DB_PATH', value: '/data/bills.db' }
    ]
  }
}

module modAnomaliesDb 'modules/container-app.bicep' = {
  name: 'deploy-anomalies-db'
  params: {
    name: 'anomalies-db'
    location: location
    environmentId: environment.id
    targetPort: 6004
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '6004' }
      { name: 'DB_PATH', value: '/app/data/anomalies.db' }
      { name: 'TRANSACTIONS_DB_URL', value: 'https://transactions-db.${envDomain}' }
      { name: 'TRANSACTIONS_TIMEOUT_SECONDS', value: '10' }
      { name: 'RECONCILE_MAX_RETRIES', value: '30' }
      { name: 'RECONCILE_RETRY_DELAY_SECONDS', value: '2' }
    ]
  }
  dependsOn: [
    modTransactionsDb
  ]
}

module modBudgetsDb 'modules/container-app.bicep' = {
  name: 'deploy-budgets-db'
  params: {
    name: 'budgets-db'
    location: location
    environmentId: environment.id
    targetPort: 6006
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '6006' }
      { name: 'DB_PATH', value: '/app/data/budgets.db' }
    ]
  }
}

module modSavingsDb 'modules/container-app.bicep' = {
  name: 'deploy-savings-db'
  params: {
    name: 'savings-db'
    location: location
    environmentId: environment.id
    targetPort: 6002
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '6002' }
      { name: 'DB_PATH', value: '/app/data/savings.db' }
    ]
  }
}

// --- Backends (Reusable Backend API Module) ---
module modTransactionsBackend 'modules/container-app.bicep' = {
  name: 'deploy-transactions-backend'
  params: {
    name: 'transactions-backend'
    location: location
    environmentId: environment.id
    targetPort: 5001
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: backendCpu
    memory: backendMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '5001' }
      { name: 'TRANSACTIONS_DB_URL', value: 'https://transactions-db.${envDomain}' }
      { name: 'DATABASE_TIMEOUT_SECONDS', value: '20' }
      { name: 'ANOMALIES_BACKEND_URL', value: 'https://anomalies-backend.${envDomain}' }
      { name: 'ANOMALIES_TIMEOUT_SECONDS', value: '10' }
      { name: 'OLLAMA_URL', value: ollamaUrl }
      { name: 'CHAT_MODEL', value: 'qwen2.5:3b' }
      { name: 'AGENT_MAX_ITERATIONS', value: '2' }
      { name: 'AGENT_TRACE_ENABLED', value: 'true' }
      { name: 'AGENT_LOG_ENABLED', value: 'true' }
      { name: 'AGENT_REQUEST_TTL_SECONDS', value: '900' }
      { name: 'AI_TIMEOUT_SECONDS', value: '90' }
      { name: 'MCP_ENABLED', value: enableMcp ? 'true' : 'false' }
      { name: 'RAG_ENABLED', value: enableRag ? 'true' : 'false' }
    ]
  }
  dependsOn: [
    modTransactionsDb
  ]
}

module modBillsBackend 'modules/container-app.bicep' = {
  name: 'deploy-bills-backend'
  params: {
    name: 'bills-backend'
    location: location
    environmentId: environment.id
    targetPort: 5005
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: backendCpu
    memory: backendMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '5005' }
      { name: 'BILLS_DB_API_URL', value: 'https://bills-db.${envDomain}' }
      { name: 'FRONTEND_ORIGIN', value: 'https://shared-frontend.${envDomain}' }
      { name: 'OLLAMA_URL', value: ollamaUrl }
      { name: 'OLLAMA_KEEP_ALIVE', value: '30m' }
      { name: 'MCP_ENABLED', value: enableMcp ? 'true' : 'false' }
      { name: 'MCP_SERVER_URL', value: 'http://host.docker.internal:8000/mcp' }
      { name: 'RAG_ENABLED', value: enableRag ? 'true' : 'false' }
    ]
  }
  dependsOn: [
    modBillsDb
  ]
}

module modAnomaliesBackend 'modules/container-app.bicep' = {
  name: 'deploy-anomalies-backend'
  params: {
    name: 'anomalies-backend'
    location: location
    environmentId: environment.id
    targetPort: 5004
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: backendCpu
    memory: backendMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '5004' }
      { name: 'ANOMALIES_DB_URL', value: 'https://anomalies-db.${envDomain}/anomalies' }
      { name: 'TRANSACTIONS_DB_URL', value: 'https://transactions-db.${envDomain}' }
      { name: 'OLLAMA_URL', value: '${ollamaUrl}/v1' }
      { name: 'OLLAMA_MODEL', value: 'llama3.1:8b' }
      { name: 'OLLAMA_LOG_LEVEL', value: 'DEBUG' }
      { name: 'MCP_ENABLED', value: enableMcp ? 'true' : 'false' }
      { name: 'MCP_SERVER_URL', value: 'http://host.docker.internal:8000/mcp' }
      { name: 'RAG_ENABLED', value: enableRag ? 'true' : 'false' }
      { name: 'RAG_SERVER_URL', value: 'http://host.docker.internal:5003' }
      { name: 'RAG_FEATURE', value: 'anomalies' }
      { name: 'RAG_TOP_K', value: '3' }
      { name: 'RAG_TIMEOUT_SECONDS', value: '15' }
    ]
  }
  dependsOn: [
    modAnomaliesDb
  ]
}

module modBudgetsBackend 'modules/container-app.bicep' = {
  name: 'deploy-budgets-backend'
  params: {
    name: 'budgets-backend'
    location: location
    environmentId: environment.id
    targetPort: 5006
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: backendCpu
    memory: backendMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '5006' }
      { name: 'BUDGETS_DB_URL', value: 'https://budgets-db.${envDomain}' }
      { name: 'TRANSACTIONS_API_URL', value: 'https://transactions-backend.${envDomain}' }
      { name: 'OLLAMA_URL', value: ollamaUrl }
      { name: 'CHAT_MODEL', value: 'qwen2.5:3b' }
      { name: 'AI_TIMEOUT_SECONDS', value: '90' }
      { name: 'MCP_ENABLED', value: enableMcp ? 'true' : 'false' }
      { name: 'MCP_SERVER_URL', value: 'http://host.docker.internal:8000/mcp' }
      { name: 'MCP_TIMEOUT_SECONDS', value: '15' }
      { name: 'RAG_ENABLED', value: enableRag ? 'true' : 'false' }
      { name: 'RAG_SERVER_URL', value: 'http://host.docker.internal:5003' }
      { name: 'RAG_FEATURE', value: 'budgets' }
      { name: 'RAG_TOP_K', value: '3' }
    ]
  }
  dependsOn: [
    modBudgetsDb
  ]
}

module modSavingsBackend 'modules/container-app.bicep' = {
  name: 'deploy-savings-backend'
  params: {
    name: 'savings-backend'
    location: location
    environmentId: environment.id
    targetPort: 5002
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: backendCpu
    memory: backendMemory
    minReplicas: minReplicas
    env: [
      { name: 'FLASK_ENV', value: 'production' }
      { name: 'PORT', value: '5002' }
      { name: 'DB_URL', value: 'https://savings-db.${envDomain}' }
      { name: 'TRANSACTIONS_DB_URL', value: 'https://transactions-db.${envDomain}' }
      { name: 'OLLAMA_URL', value: '${ollamaUrl}/v1' }
      { name: 'OLLAMA_MODEL', value: 'llama3.1:8b' }
      { name: 'OLLAMA_CLASSIFIER_MODEL', value: 'qwen2.5:3b' }
      { name: 'MCP_ENABLED', value: 'false' }
      { name: 'MCP_SERVER_URL', value: 'http://host.docker.internal:8000/mcp' }
      { name: 'RAG_ENABLED', value: 'false' }
      { name: 'MULTI_AGENT_ENABLED', value: 'false' }
    ]
  }
  dependsOn: [
    modSavingsDb
    modTransactionsDb
  ]
}

// --- Frontends (Reusable Frontend Module) ---
module modTransactionsFrontend 'modules/container-app.bicep' = {
  name: 'deploy-transactions-frontend'
  params: {
    name: 'transactions-frontend'
    location: location
    environmentId: environment.id
    targetPort: 3001
    externalIngress: false
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '3001' }
      { name: 'TRANSACTIONS_BACKEND_URL', value: 'https://transactions-backend.${envDomain}' }
    ]
  }
  dependsOn: [
    modTransactionsBackend
  ]
}

module modBillsFrontend 'modules/container-app.bicep' = {
  name: 'deploy-bills-frontend'
  params: {
    name: 'bills-frontend'
    location: location
    environmentId: environment.id
    targetPort: 80
    externalIngress: false
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'BILLS_BACKEND_URL', value: 'https://bills-backend.${envDomain}' }
    ]
  }
  dependsOn: [
    modBillsBackend
  ]
}

module modAnomaliesFrontend 'modules/container-app.bicep' = {
  name: 'deploy-anomalies-frontend'
  params: {
    name: 'anomalies-frontend'
    location: location
    environmentId: environment.id
    targetPort: 3004
    externalIngress: false
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '3004' }
      { name: 'ANOMALIES_BACKEND_URL', value: 'https://anomalies-backend.${envDomain}' }
    ]
  }
  dependsOn: [
    modAnomaliesBackend
  ]
}

module modBudgetsFrontend 'modules/container-app.bicep' = {
  name: 'deploy-budgets-frontend'
  params: {
    name: 'budgets-frontend'
    location: location
    environmentId: environment.id
    targetPort: 3006
    externalIngress: false
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '3006' }
      { name: 'BUDGETS_BACKEND_URL', value: 'https://budgets-backend.${envDomain}' }
    ]
  }
  dependsOn: [
    modBudgetsBackend
  ]
}

module modSavingsFrontend 'modules/container-app.bicep' = {
  name: 'deploy-savings-frontend'
  params: {
    name: 'savings-frontend'
    location: location
    environmentId: environment.id
    targetPort: 3002
    externalIngress: false
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '3002' }
      { name: 'BACKEND_URL', value: 'https://savings-backend.${envDomain}' }
    ]
  }
  dependsOn: [
    modSavingsBackend
  ]
}

// --- Shared Gateway Frontend (External Ingress = true) ---
module modSharedFrontend 'modules/container-app.bicep' = {
  name: 'deploy-shared-frontend'
  params: {
    name: 'shared-frontend'
    location: location
    environmentId: environment.id
    targetPort: 3000
    externalIngress: true // The only public endpoint
    acrLoginServer: acrLoginServer
    acrAdminUsername: acrAdminUsername
    acrAdminPassword: acrAdminPassword
    imageTag: imageTag
    cpu: defaultCpu
    memory: defaultMemory
    minReplicas: minReplicas
    env: [
      { name: 'PORT', value: '3000' }
      { name: 'TRANSACTIONS_FRONTEND_URL', value: 'https://transactions-frontend.${envDomain}' }
      { name: 'ANOMALIES_FRONTEND_URL', value: 'https://anomalies-frontend.${envDomain}' }
      { name: 'SAVINGS_FRONTEND_URL', value: 'https://savings-frontend.${envDomain}' }
      { name: 'BILLS_FRONTEND_URL', value: 'https://bills-frontend.${envDomain}' }
      { name: 'BUDGETS_FRONTEND_URL', value: 'https://budgets-frontend.${envDomain}' }
    ]
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
// 5. Outputs
// ==============================================================================

@description('The login server for the Azure Container Registry')
output acrLoginServer string = acr.properties.loginServer

@description('Public URL to access the Tally shared frontend (stable FQDN)')
output applicationUrl string = 'https://${modSharedFrontend.outputs.fqdn}'

@description('Public URL targeting the latest revision of the shared frontend')
output applicationLatestRevisionUrl string = 'https://${modSharedFrontend.outputs.latestRevisionFqdn}'

@description('Default domain for the Azure Container Apps environment')
output environmentDefaultDomain string = environment.properties.defaultDomain

@description('Base URL for the external Ollama VM API')
output ollamaUrl string = ollamaUrl

@description('FQDN of the dedicated Ollama VM (empty if deployOllamaVm is false)')
output ollamaVmFqdn string = modOllamaVm.?outputs.fqdn ?? ''
