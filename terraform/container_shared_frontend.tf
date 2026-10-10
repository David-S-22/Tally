# ==============================================================================
# Container App: shared-frontend
# Public-facing web UI / Gateway Reverse Proxy
# ==============================================================================

resource "azurerm_container_app" "shared_frontend" {
  name                         = "shared-frontend"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = true # The only public endpoint
    target_port                = 3000
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
      name   = "shared-frontend"
      image  = "${local.acr_login_server}/shared-frontend:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      # Addresses Item 1: Internal service discovery URLs do NOT use ports.
      # ACA internal Envoy proxy routes http://<app-name> to the target_port.
      env {
        name  = "PORT"
        value = "3000"
      }
      env {
        name  = "TRANSACTIONS_FRONTEND_URL"
        value = "http://transactions-frontend"
      }
      env {
        name  = "ANOMALIES_FRONTEND_URL"
        value = "http://anomalies-frontend"
      }
      env {
        name  = "SAVINGS_FRONTEND_URL"
        value = "http://savings-frontend"
      }
      env {
        name  = "BILLS_FRONTEND_URL"
        value = "http://bills-frontend"
      }
      env {
        name  = "BUDGETS_FRONTEND_URL"
        value = "http://budgets-frontend"
      }
    }
  }

  # Dependency Order: All feature frontends must be provisioned before the shared gateway frontend
  depends_on = [
    azurerm_container_app.transactions_frontend,
    azurerm_container_app.bills_frontend,
    azurerm_container_app.anomalies_frontend,
    azurerm_container_app.budgets_frontend,
    azurerm_container_app.savings_frontend
  ]
}
