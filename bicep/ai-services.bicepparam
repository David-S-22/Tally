using 'ai-services.bicep'

param adminPublicKey = readEnvironmentVariable('AI_VM_SSH_PUBLIC_KEY')
param containerDomain = readEnvironmentVariable('CONTAINER_DOMAIN')
param multiAgentToken = readEnvironmentVariable('MULTI_AGENT_TOKEN')
