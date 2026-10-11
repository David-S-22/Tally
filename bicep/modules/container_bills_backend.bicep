// ==============================================================================
// Container App: bills-backend
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
  name: 'bills-backend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 5005
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
          name: 'bills-backend'
          image: '${acrLoginServer}/bills-backend:${imageTag}'
          resources: {
            cpu: json(defaultCpu)
            memory: defaultMemory
          }
          // Internal service discovery URLs without ports
          env: [
            {
              name: 'PORT'
              value: '5005'
            }
            {
              name: 'BILLS_DB_API_URL'
              value: 'http://bills-db'
            }
            {
              name: 'FRONTEND_ORIGIN'
              value: 'http://shared-frontend'
            }
            {
              name: 'OLLAMA_URL'
              value: 'http://ollama'
            }
            {
              name: 'OLLAMA_KEEP_ALIVE'
              value: '30m'
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
