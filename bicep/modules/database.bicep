// ==============================================================================
// Module: SQLite Database Microservice
// Reusable template for all SQLite container services with Azure Files persistent volume.
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
param mountPath string = '/app/data'
param dbStorageName string = 'db-storage'
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
      volumes: [
        {
          name: 'db-vol'
          storageType: 'AzureFile'
          storageName: dbStorageName
        }
      ]
      containers: [
        {
          name: name
          image: '${acrLoginServer}/${name}:${imageTag}'
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: env
          volumeMounts: [
            {
              volumeName: 'db-vol'
              mountPath: mountPath
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
