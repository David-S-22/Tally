// ==============================================================================
// Container App: shared-frontend
// Public-facing web UI / Gateway Reverse Proxy
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
  name: 'shared-frontend'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true // The only public endpoint
        targetPort: 3000
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
          name: 'shared-frontend'
          image: '${acrLoginServer}/shared-frontend:${imageTag}'
          resources: {
            cpu: json(defaultCpu)
            memory: defaultMemory
          }
          // Internal service discovery URLs do NOT use ports.
          // ACA internal Envoy proxy routes http://<app-name> to the target_port.
          env: [
            {
              name: 'PORT'
              value: '3000'
            }
            {
              name: 'TRANSACTIONS_FRONTEND_URL'
              value: 'http://transactions-frontend'
            }
            {
              name: 'ANOMALIES_FRONTEND_URL'
              value: 'http://anomalies-frontend'
            }
            {
              name: 'SAVINGS_FRONTEND_URL'
              value: 'http://savings-frontend'
            }
            {
              name: 'BILLS_FRONTEND_URL'
              value: 'http://bills-frontend'
            }
            {
              name: 'BUDGETS_FRONTEND_URL'
              value: 'http://budgets-frontend'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
output fqdn string = containerApp.properties.configuration.ingress.fqdn
output latestRevisionFqdn string = containerApp.properties.latestRevisionFqdn
