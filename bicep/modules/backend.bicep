// ==============================================================================
// Module: Backend API Microservice
// Reusable template for all backend API container services.
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
param env array = []
param cpu string = '2.0'
param memory string = '4Gi'
param minReplicas int = 0
param maxReplicas int = 1

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: targetPort
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
        maxReplicas: maxReplicas
      }
      containers: [
        {
          name: name
          image: '${acrLoginServer}/${name}:${imageTag}'
          resources: {
            cpu: json(cpu)
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
