# ==============================================================================
# Container App: transactions-backend
# ==============================================================================

resource "azurerm_container_app" "transactions_backend" {
  name                         = "transactions-backend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 5001
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
      name   = "transactions-backend"
      image  = "${local.acr_login_server}/transactions-backend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs without ports
      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "5001"
      }
      env {
        name  = "TRANSACTIONS_DB_URL"
        value = "http://transactions-db"
      }
      env {
        name  = "DATABASE_TIMEOUT_SECONDS"
        value = "20"
      }
      env {
        name  = "ANOMALIES_BACKEND_URL"
        value = "http://anomalies-backend"
      }
      env {
        name  = "ANOMALIES_TIMEOUT_SECONDS"
        value = "10"
      }
      env {
        name  = "OLLAMA_URL"
        value = "http://ollama"
      }
      env {
        name  = "CHAT_MODEL"
        value = "qwen2.5:3b"
      }
      env {
        name  = "AGENT_MAX_ITERATIONS"
        value = "2"
      }
      env {
        name  = "AGENT_TRACE_ENABLED"
        value = "true"
      }
      env {
        name  = "AGENT_LOG_ENABLED"
        value = "true"
      }
      env {
        name  = "AGENT_REQUEST_TTL_SECONDS"
        value = "900"
      }
      env {
        name  = "AI_TIMEOUT_SECONDS"
        value = "90"
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

  # Dependency Order: Database and Ollama must be provisioned before backend
  depends_on = [
    azurerm_container_app.transactions_db,
    azurerm_container_app.ollama
  ]
}
