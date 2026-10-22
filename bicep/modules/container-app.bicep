// ==============================================================================
// Module: Container App Microservice
// Unified reusable template for database, backend API, and frontend UI services.
// ==============================================================================

param name string
param location string = resourceGroup().location
param environmentId string
param acrLoginServer string
param acrAdminUsername string
@secure()
param acrAdminPassword string
param imageTag string = 'latest'
param targetPort int
param externalIngress bool = false
param env array = []
@secure()
param appSecrets object = {}
param cpu string = '0.25'
param memory string = '0.5Gi'
param minReplicas int = 0
param maxReplicas int = 1

var additionalSecrets = [for secret in items(appSecrets): {
  name: secret.key
  value: secret.value
}]

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: externalIngress
        targetPort: targetPort
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: concat([
        {
          name: 'acr-password'
          value: acrAdminPassword
        }
      ], additionalSecrets)
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
        maxReplicas: maxReplicas
      }
      containers: [
        {
          name: name
          image: '${acrLoginServer}/${name}:${imageTag}'
          resources: {
            cpu: any(cpu)
            memory: memory
          }
          env: env
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
output fqdn string = externalIngress ? containerApp.properties.configuration.ingress.fqdn : ''
output latestRevisionFqdn string = externalIngress ? containerApp.properties.latestRevisionFqdn : ''
