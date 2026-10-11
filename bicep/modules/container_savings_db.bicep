// ==============================================================================
// Container App: savings-db
// ==============================================================================

param location string = resourceGroup().location
param environmentId string
param dbStorageName string = 'db-storage'
param acrLoginServer string
param acrAdminUsername string
@secure()
param acrAdminPassword string
param imageTag string = 'latest'
param minReplicas int = 0
param defaultCpu string = '2.0'
param defaultMemory string = '4Gi'

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'savings-db'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: false
        targetPort: 6002
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
      volumes: [
        {
          name: 'db-vol'
          storageType: 'AzureFile'
          storageName: dbStorageName
        }
      ]
      containers: [
        {
          name: 'savings-db'
          image: '${acrLoginServer}/savings-db:${imageTag}'
          resources: {
            cpu: json(defaultCpu)
            memory: defaultMemory
          }
          env: [
            {
              name: 'FLASK_ENV'
              value: 'production'
            }
            {
              name: 'PORT'
              value: '6002'
            }
            {
              name: 'DB_PATH'
              value: '/app/data/savings.db'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'db-vol'
              mountPath: '/app/data'
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
