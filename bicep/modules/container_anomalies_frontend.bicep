// ==============================================================================
// Container App: anomalies-frontend
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

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'anomalies-frontend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 3004
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
          name: 'anomalies-frontend'
          image: '${acrLoginServer}/anomalies-frontend:${imageTag}'
          resources: {
            cpu: json(defaultCpu)
            memory: defaultMemory
          }
          env: [
            {
              name: 'PORT'
              value: '3004'
            }
            {
              name: 'ANOMALIES_BACKEND_URL'
              value: 'http://anomalies-backend'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
