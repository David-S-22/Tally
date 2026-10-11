// ==============================================================================
// Container App: ollama
// Pulls raw image directly from Docker Hub — NO custom Dockerfile needed.
// Auto-pulls models on startup via inline shell command and persists to Azure Files.
// ==============================================================================

param location string = resourceGroup().location
param environmentId string
param workloadProfileName string = 'ollama-profile'
param ollamaStorageName string = 'ollama-storage'
param ollamaCpu string = '4.0'
param ollamaMemory string = '16Gi'
param ollamaMinReplicas int = 0
param ollamaImage string = 'ollama/ollama:latest'
param ollamaModels string = 'qwen2.5:0.5b llama3.1:8b qwen2.5:3b qwen3:4b'

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ollama'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: workloadProfileName
    configuration: {
      ingress: {
        external: false
        targetPort: 11434
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
        minReplicas: ollamaMinReplicas
        maxReplicas: 1
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
          image: ollamaImage
          resources: {
            cpu: json(ollamaCpu)
            memory: ollamaMemory
          }
          // Inline startup command replaces the custom Dockerfile entrypoint script.
          // Starts ollama serve in background, waits for daemon readiness, pulls all models
          // listed in OLLAMA_PULL_MODELS into the persistent volume, then waits on the daemon.
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
              value: ollamaModels
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
                port: 11434
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
