// ==============================================================================
// Container App: transactions-backend
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
  name: 'transactions-backend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 5001
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
          name: 'transactions-backend'
          image: '${acrLoginServer}/transactions-backend:${imageTag}'
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
              value: '5001'
            }
            {
              name: 'TRANSACTIONS_DB_URL'
              value: 'http://transactions-db'
            }
            {
              name: 'DATABASE_TIMEOUT_SECONDS'
              value: '20'
            }
            {
              name: 'ANOMALIES_BACKEND_URL'
              value: 'http://anomalies-backend'
            }
            {
              name: 'ANOMALIES_TIMEOUT_SECONDS'
              value: '10'
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
              name: 'AGENT_MAX_ITERATIONS'
              value: '2'
            }
            {
              name: 'AGENT_TRACE_ENABLED'
              value: 'true'
            }
            {
              name: 'AGENT_LOG_ENABLED'
              value: 'true'
            }
            {
              name: 'AGENT_REQUEST_TTL_SECONDS'
              value: '900'
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
              name: 'RAG_ENABLED'
              value: enableRag ? 'true' : 'false'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
