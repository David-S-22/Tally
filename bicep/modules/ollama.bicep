// ==============================================================================
// Module: Ollama LLM Engine
// Runs on dedicated workload profile with persistent volume for models.
// ==============================================================================

param location string = resourceGroup().location
param environmentId string
param workloadProfileName string = 'ollama-profile'
param ollamaStorageName string = 'ollama-storage'
param cpu string = '4.0'
param memory string = '16Gi'
param minReplicas int = 0
param maxReplicas int = 1
param image string = 'ollama/ollama:latest'
param models string = 'qwen2.5:0.5b llama3.1:8b qwen2.5:3b qwen3:4b'
param targetPort int = 11434

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ollama'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: workloadProfileName
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
    }
    template: {
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
      volumes: [
        {
          name: 'ollama-vol'
          storageType: 'AzureFile'
          storageName: ollamaStorageName
        }
      ]
      containers: [
        {
          name: 'ollama'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'ollama serve & pid=$! ; echo \'[START] Waiting for ollama daemon...\' ; until ollama list >/dev/null 2>&1; do sleep 1; done ; echo \'[END] Waiting for ollama daemon.\' ; echo \'[START] Pulling models...\' ; for m in $OLLAMA_PULL_MODELS; do echo "[PULL] $m" ; ollama pull "$m" ; done ; echo \'[END] Pulling models.\' ; wait $pid'
          ]
          env: [
            {
              name: 'OLLAMA_PULL_MODELS'
              value: models
            }
          ]
          volumeMounts: [
            {
              volumeName: 'ollama-vol'
              mountPath: '/root/.ollama'
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: targetPort
              }
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 10
              successThreshold: 1
            }
          ]
        }
      ]
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
