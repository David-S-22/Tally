using 'main.bicep'

param location = 'australiaeast'
param acrName = 'tallyasd21'
param environmentName = 'cae-tally'
param storageAccountPrefix = 'sttally'

// Scale-to-Zero Configuration
param minReplicas = 0 // Set to 0 to enable scale-to-zero when idle

// Ollama Configuration (pulls raw image directly from Docker Hub — NO custom Dockerfile needed)
param ollamaImage = 'ollama/ollama:latest'
param ollamaWorkloadProfileType = 'D4' // Dedicated compute profile for Ollama
param ollamaCpu = '3.5'
param ollamaMemory = '12Gi'
param ollamaMinReplicas = 0 // Set to 0 to enable scale-to-zero when idle
param ollamaModels = 'qwen2.5:0.5b llama3.1:8b qwen2.5:3b qwen3:4b'

// Standard Microservices Resources (2 vCPU / 4Gi RAM per container)
param defaultCpu = 2
param defaultMemory = '4Gi'
param imageTag = 'latest'

// Optional feature flags
param enableMcp = false
param enableRag = false
