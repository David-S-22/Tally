# ==============================================================================
# Container App: transactions-frontend
# ==============================================================================

resource "azurerm_container_app" "transactions_frontend" {
  name                         = "transactions-frontend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 3001
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
      name   = "transactions-frontend"
      image  = "${local.acr_login_server}/transactions-frontend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      env {
        name  = "PORT"
        value = "3001"
      }
      env {
        name  = "TRANSACTIONS_BACKEND_URL"
        value = "http://transactions-backend"
      }
    }
  }

  # Dependency Order: Backend must be provisioned before frontend
  depends_on = [
    azurerm_container_app.transactions_backend
  ]
}
