// ==============================================================================
// Container App: anomalies-backend
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
  name: 'anomalies-backend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 5004
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
          name: 'anomalies-backend'
          image: '${acrLoginServer}/anomalies-backend:${imageTag}'
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
              value: '5004'
            }
            {
              name: 'ANOMALIES_DB_URL'
              value: 'http://anomalies-db/anomalies'
            }
            {
              name: 'TRANSACTIONS_DB_URL'
              value: 'http://transactions-db'
            }
            {
              name: 'OLLAMA_URL'
              value: 'http://ollama/v1'
            }
            {
              name: 'OLLAMA_MODEL'
              value: 'llama3.1:8b'
            }
            {
              name: 'OLLAMA_LOG_LEVEL'
              value: 'DEBUG'
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
              name: 'RAG_ENABLED'
              value: enableRag ? 'true' : 'false'
            }
            {
              name: 'RAG_SERVER_URL'
              value: 'http://host.docker.internal:5003'
            }
            {
              name: 'RAG_FEATURE'
              value: 'anomalies'
            }
            {
              name: 'RAG_TOP_K'
              value: '3'
            }
            {
              name: 'RAG_TIMEOUT_SECONDS'
              value: '15'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
