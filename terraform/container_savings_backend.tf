# ==============================================================================
# Container App: savings-backend
# ==============================================================================

resource "azurerm_container_app" "savings_backend" {
  name                         = "savings-backend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 5002
    allow_insecure_connections = true

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  secret {
    name  = "acr-password"
    value = local.acr_admin_password
  }

  registry {
    server               = local.acr_login_server
    username             = local.acr_admin_username
    password_secret_name = "acr-password"
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = 1

    container {
      name   = "savings-backend"
      image  = "${local.acr_login_server}/savings-backend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs without ports
      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "5002"
      }
      env {
        name  = "DB_URL"
        value = "http://savings-db"
      }
      env {
        name  = "TRANSACTIONS_DB_URL"
        value = "http://transactions-db"
      }
      env {
        name  = "OLLAMA_URL"
        value = "http://ollama/v1"
      }
      env {
        name  = "OLLAMA_MODEL"
        value = "llama3.1:8b"
      }
      env {
        name  = "OLLAMA_CLASSIFIER_MODEL"
        value = "qwen2.5:3b"
      }
      env {
        name  = "MCP_ENABLED"
        value = var.enable_mcp ? "true" : "false"
      }
      env {
        name  = "RAG_ENABLED"
        value = var.enable_rag ? "true" : "false"
      }
    }
  }

  # Dependency Order: Savings DB, Transactions DB, and Ollama must be provisioned before backend
  depends_on = [
    azurerm_container_app.savings_db,
    azurerm_container_app.transactions_db,
    azurerm_container_app.ollama
  ]
}
