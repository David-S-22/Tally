# ==============================================================================
# Container App: bills-backend
# ==============================================================================

resource "azurerm_container_app" "bills_backend" {
  name                         = "bills-backend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 5005
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
      name   = "bills-backend"
      image  = "${local.acr_login_server}/bills-backend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs without ports
      env {
        name  = "PORT"
        value = "5005"
      }
      env {
        name  = "BILLS_DB_API_URL"
        value = "http://bills-db"
      }
      env {
        name  = "FRONTEND_ORIGIN"
        value = "http://shared-frontend"
      }
      env {
        name  = "OLLAMA_URL"
        value = "http://ollama"
      }
      env {
        name  = "OLLAMA_KEEP_ALIVE"
        value = "30m"
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
    }
  }

  # Dependency Order: Database and Ollama must be provisioned before backend
  depends_on = [
    azurerm_container_app.bills_db,
    azurerm_container_app.ollama
  ]
}
