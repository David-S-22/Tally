# ==============================================================================
# Container App: budgets-backend
# ==============================================================================

resource "azurerm_container_app" "budgets_backend" {
  name                         = "budgets-backend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 5006
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
      name   = "budgets-backend"
      image  = "${local.acr_login_server}/budgets-backend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs without ports
      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "5006"
      }
      env {
        name  = "BUDGETS_DB_URL"
        value = "http://budgets-db"
      }
      # Required for inter-service communication; prevents fallback to :5001 default
      env {
        name  = "TRANSACTIONS_API_URL"
        value = "http://transactions-backend"
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
        name  = "AI_TIMEOUT_SECONDS"
        value = "90"
      }
      env {
        name  = "MCP_ENABLED"
        value = var.enable_mcp ? "true" : "false"
      }
      env {
        name  = "MCP_SERVER_URL"
        value = "http://host.docker.internal:8000/mcp"
      }
      env {
        name  = "MCP_TIMEOUT_SECONDS"
        value = "15"
      }
      env {
        name  = "RAG_ENABLED"
        value = var.enable_rag ? "true" : "false"
      }
      env {
        name  = "RAG_SERVER_URL"
        value = "http://host.docker.internal:5003"
      }
      env {
        name  = "RAG_FEATURE"
        value = "budgets"
      }
      env {
        name  = "RAG_TOP_K"
        value = "3"
      }
    }
  }

  # Dependency Order: Follows docker-compose.yml depends_on: [budgets-db, ollama]
  depends_on = [
    azurerm_container_app.budgets_db,
    azurerm_container_app.ollama
  ]
}
