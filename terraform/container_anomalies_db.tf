# ==============================================================================
# Container App: anomalies-db
# ==============================================================================

resource "azurerm_container_app" "anomalies_db" {
  name                         = "anomalies-db"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 6004
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

    volume {
      name         = "db-vol"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.db_storage.name
    }

    container {
      name   = "anomalies-db"
      image  = "${local.acr_login_server}/anomalies-db:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "6004"
      }
      env {
        name  = "DB_PATH"
        value = "/app/data/anomalies.db"
      }
      env {
        name  = "TRANSACTIONS_DB_URL"
        value = "http://transactions-db"
      }
      env {
        name  = "TRANSACTIONS_TIMEOUT_SECONDS"
        value = "10"
      }
      env {
        name  = "RECONCILE_MAX_RETRIES"
        value = "30"
      }
      env {
        name  = "RECONCILE_RETRY_DELAY_SECONDS"
        value = "2"
      }

      volume_mounts {
        name = "db-vol"
        path = "/app/data"
      }
    }
  }

  # Dependency Order: Storage and transactions-db (for startup reconciliation)
  depends_on = [
    azurerm_container_app_environment_storage.db_storage,
    azurerm_container_app.transactions_db
  ]
}
