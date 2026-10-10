terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.90"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}

# ==============================================================================
# 1. Base Infrastructure: Resource Group, Container Registry, Log Analytics
# ==============================================================================

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

resource "azurerm_container_registry" "acr" {
  name                = lower(var.acr_name)
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Standard"
  admin_enabled       = true
}

resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${var.environment_name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
}

# ==============================================================================
# 2. Persistent Storage (Azure Files — replaces docker-compose volumes)
# ==============================================================================

resource "random_string" "storage_suffix" {
  length  = 5
  special = false
  upper   = false
}

resource "azurerm_storage_account" "storage" {
  name                     = "sttally${random_string.storage_suffix.result}"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

# Replaces docker-compose volume: ollama_data -> /root/.ollama
resource "azurerm_storage_share" "ollama_share" {
  name                 = "ollama-data"
  storage_account_name = azurerm_storage_account.storage.name
  quota                = 100 # 100 GB for LLM model weights
}

# Replaces docker-compose volumes: transactions_data, bills_data, anomalies_data, etc.
resource "azurerm_storage_share" "db_share" {
  name                 = "db-data"
  storage_account_name = azurerm_storage_account.storage.name
  quota                = 10
}

# ==============================================================================
# 3. Azure Container Apps Managed Environment
# ==============================================================================

resource "azurerm_container_app_environment" "env" {
  name                       = var.environment_name
  location                   = azurerm_resource_group.rg.location
  resource_group_name        = azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id

  # Serverless profile for all standard microservices (max 4 vCPU / 8Gi per app)
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  # Dedicated profile for Ollama — LLM inference needs more CPU/RAM than
  # the Consumption profile allows (which caps at 4 vCPU / 8Gi)
  workload_profile {
    name                  = "ollama-profile"
    workload_profile_type = var.ollama_workload_profile_type
    minimum_count         = 1
    maximum_count         = 2
  }
}

# Link Azure File Shares into the Container Apps Environment so containers
# can mount them as volumes (equivalent to docker-compose named volumes)
resource "azurerm_container_app_environment_storage" "ollama_storage" {
  name                         = "ollama-storage"
  container_app_environment_id = azurerm_container_app_environment.env.id
  account_name                 = azurerm_storage_account.storage.name
  share_name                   = azurerm_storage_share.ollama_share.name
  access_key                   = azurerm_storage_account.storage.primary_access_key
  access_mode                  = "ReadWrite"
}

resource "azurerm_container_app_environment_storage" "db_storage" {
  name                         = "db-storage"
  container_app_environment_id = azurerm_container_app_environment.env.id
  account_name                 = azurerm_storage_account.storage.name
  share_name                   = azurerm_storage_share.db_share.name
  access_key                   = azurerm_storage_account.storage.primary_access_key
  access_mode                  = "ReadWrite"
}

# ==============================================================================
# 4. Common Locals
# ==============================================================================

locals {
  acr_login_server   = azurerm_container_registry.acr.login_server
  acr_admin_username = azurerm_container_registry.acr.admin_username
  acr_admin_password = azurerm_container_registry.acr.admin_password
}
