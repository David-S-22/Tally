// ==============================================================================
// Module: Ollama LLM Engine
// Runs on dedicated workload profile with persistent volume for models.
// ==============================================================================

param location string = resourceGroup().location
param environmentId string
param workloadProfileName string = 'ollama-profile'
param ollamaStorageName string = 'ollama-storage'
param cpu string = '3.5'
param memory string = '12Gi'
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
            cpu: any(cpu)
            memory: memory
          }
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'OLLAMA_HOST=127.0.0.1:11435 ollama serve & pid=$! ; echo "[START] Waiting for internal ollama daemon..." ; until OLLAMA_HOST=127.0.0.1:11435 ollama list >/dev/null 2>&1; do sleep 1; done ; echo "[END] Waiting for internal ollama daemon." ; echo "[START] Pulling models..." ; for m in $OLLAMA_PULL_MODELS; do echo "[PULL] $m" ; until OLLAMA_HOST=127.0.0.1:11435 ollama pull "$m"; do echo "[RETRY] Failed pulling $m, retrying in 3s..." ; sleep 3 ; done ; done ; echo "[END] Pulling models. All models verified on disk." ; kill $pid ; wait $pid 2>/dev/null ; echo "[READY] Starting public Ollama server on port 11434..." ; exec ollama serve'
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
              type: 'Startup'
              httpGet: {
                path: '/'
                port: targetPort
              }
              initialDelaySeconds: 10
              periodSeconds: 60
              timeoutSeconds: 5
              failureThreshold: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: targetPort
              }
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 3
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
