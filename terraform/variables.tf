variable "resource_group_name" {
  description = "Name of the Azure Resource Group"
  type        = string
  default     = "ASD-GROUP-21"
}

variable "location" {
  description = "Azure region for deployment"
  type        = string
  default     = "australiaeast"
}

variable "acr_name" {
  description = "Name of the Azure Container Registry (must be lowercase alphanumeric, 5-50 chars)"
  type        = string
  default     = "tallyasd21"
}

variable "environment_name" {
  description = "Name of the Azure Container Apps Managed Environment"
  type        = string
  default     = "cae-tally"
}

variable "min_replicas" {
  description = "Minimum number of replicas for container apps (0 enables scale-to-zero when idle)"
  type        = number
  default     = 0
}

variable "ollama_workload_profile_type" {
  description = "Workload profile compute SKU for Ollama (e.g. D4, D8, D16). Defaults to D4 to fit within standard Azure regional core limits (e.g. 4 cores on Azure for Students)."
  type        = string
  default     = "D4"
}

variable "ollama_cpu" {
  description = "vCPU allocated to Ollama (must leave room for node system overhead, e.g. max ~3.5 on D4)"
  type        = number
  default     = 3.0
}

variable "ollama_memory" {
  description = "Memory allocated to Ollama (must leave room for node system overhead, e.g. max ~14Gi on D4)"
  type        = string
  default     = "12Gi"
}

variable "ollama_min_replicas" {
  description = "Minimum replicas for Ollama. Defaults to 1 since the dedicated VM profile is billed continuously and scaling to 0 causes long model verification cold-starts."
  type        = number
  default     = 1
}

variable "ollama_image" {
  description = "Container image for Ollama (pulls raw image directly from Docker Hub; no custom Dockerfile needed)"
  type        = string
  default     = "ollama/ollama:latest"
}

variable "ollama_models" {
  description = "Models for Ollama to pull on startup"
  type        = string
  default     = "qwen2.5:0.5b llama3.1:8b qwen2.5:3b qwen3:4b"
}

variable "default_cpu" {
  description = "vCPU allocated to each standard microservice container (must maintain valid ACA CPU:memory ratio, e.g. 0.5 with 1Gi, 1.0 with 2Gi, 2.0 with 4Gi)"
  type        = number
  default     = 0.5
}

variable "default_memory" {
  description = "Memory allocated to each standard microservice container (must maintain valid ACA CPU:memory ratio)"
  type        = string
  default     = "1Gi"
}

variable "image_tag" {
  description = "Image tag to deploy for application containers"
  type        = string
  default     = "latest"
}

variable "enable_mcp" {
  description = "Enable Model Context Protocol (MCP) integrations in backend services. Defaults to false in cloud deployment unless an MCP server is configured."
  type        = bool
  default     = false
}

variable "enable_rag" {
  description = "Enable Retrieval-Augmented Generation (RAG) integrations in backend services. Defaults to false in cloud deployment unless a RAG server is configured."
  type        = bool
  default     = false
}
