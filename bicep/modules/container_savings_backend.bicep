// ==============================================================================
// Container App: savings-backend
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
  name: 'savings-backend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 5002
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
          name: 'savings-backend'
          image: '${acrLoginServer}/savings-backend:${imageTag}'
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
              value: '5002'
            }
            {
              name: 'DB_URL'
              value: 'http://savings-db'
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
              name: 'OLLAMA_CLASSIFIER_MODEL'
              value: 'qwen2.5:3b'
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
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
