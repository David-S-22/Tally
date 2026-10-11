// ==============================================================================
// Container App: budgets-backend
// ==============================================================================

param location string = resourceGroup().location
param environmentId string
param acrLoginServer string
param acrAdminUsername string
@secure()
param acrAdminPassword string
param imageTag string = 'latest'
param minReplicas int = 0
param defaultCpu string = '2.0'
param defaultMemory string = '4Gi'
param enableMcp bool = false
param enableRag bool = false

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'budgets-backend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 5006
        allowInsecure: true
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: [
        {
          name: 'acr-password'
          value: acrAdminPassword
        }
      ]
      registries: [
        {
          server: acrLoginServer
          username: acrAdminUsername
          passwordSecretRef: 'acr-password'
        }
      ]
    }
    template: {
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
      }
      containers: [
        {
          name: 'budgets-backend'
          image: '${acrLoginServer}/budgets-backend:${imageTag}'
          resources: {
            cpu: json(defaultCpu)
            memory: defaultMemory
          }
          // Internal service discovery URLs without ports
          env: [
            {
              name: 'FLASK_ENV'
              value: 'production'
            }
            {
              name: 'PORT'
              value: '5006'
            }
            {
              name: 'BUDGETS_DB_URL'
              value: 'http://budgets-db'
            }
            // Required for inter-service communication; prevents fallback to :5001 default
            {
              name: 'TRANSACTIONS_API_URL'
              value: 'http://transactions-backend'
            }
            {
              name: 'OLLAMA_URL'
              value: 'http://ollama'
            }
            {
              name: 'CHAT_MODEL'
              value: 'qwen2.5:3b'
            }
            {
              name: 'AI_TIMEOUT_SECONDS'
              value: '90'
            }
            {
              name: 'MCP_ENABLED'
              value: enableMcp ? 'true' : 'false'
            }
            {
              name: 'MCP_SERVER_URL'
              value: 'http://host.docker.internal:8000/mcp'
            }
            {
              name: 'MCP_TIMEOUT_SECONDS'
              value: '15'
            }
            {
              name: 'RAG_ENABLED'
              value: enableRag ? 'true' : 'false'
            }
            {
              name: 'RAG_SERVER_URL'
              value: 'http://host.docker.internal:5003'
            }
            {
              name: 'RAG_FEATURE'
              value: 'budgets'
            }
            {
              name: 'RAG_TOP_K'
              value: '3'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
