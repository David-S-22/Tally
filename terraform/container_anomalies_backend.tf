# ==============================================================================
# Container App: anomalies-backend
# ==============================================================================

resource "azurerm_container_app" "anomalies_backend" {
  name                         = "anomalies-backend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 5004
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
      name   = "anomalies-backend"
      image  = "${local.acr_login_server}/anomalies-backend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs without ports
      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "5004"
      }
      env {
        name  = "ANOMALIES_DB_URL"
        value = "http://anomalies-db/anomalies"
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
        name  = "OLLAMA_LOG_LEVEL"
        value = "DEBUG"
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
        name  = "RAG_ENABLED"
        value = var.enable_rag ? "true" : "false"
      }
      env {
        name  = "RAG_SERVER_URL"
        value = "http://host.docker.internal:5003"
      }
      env {
        name  = "RAG_FEATURE"
        value = "anomalies"
      }
      env {
        name  = "RAG_TOP_K"
        value = "3"
      }
      env {
        name  = "RAG_TIMEOUT_SECONDS"
        value = "15"
      }
    }
  }

  # Dependency Order: Follows docker-compose.yml depends_on: [anomalies-db, ollama]
  depends_on = [
    azurerm_container_app.anomalies_db,
    azurerm_container_app.ollama
  ]
}
