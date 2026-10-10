# ==============================================================================
# Container App: savings-frontend
# ==============================================================================

resource "azurerm_container_app" "savings_frontend" {
  name                         = "savings-frontend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 3002
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
      name   = "savings-frontend"
      image  = "${local.acr_login_server}/savings-frontend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      env {
        name  = "PORT"
        value = "3002"
      }
      env {
        name  = "BACKEND_URL"
        value = "http://savings-backend"
      }
    }
  }

  # Dependency Order: Follows docker-compose.yml depends_on: [savings-backend, ollama]
  depends_on = [
    azurerm_container_app.savings_backend,
    azurerm_container_app.ollama
  ]
}
