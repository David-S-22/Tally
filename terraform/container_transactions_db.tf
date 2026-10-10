# ==============================================================================
# Container App: transactions-db
# ==============================================================================

resource "azurerm_container_app" "transactions_db" {
  name                         = "transactions-db"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 6001
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
      name   = "transactions-db"
      image  = "${local.acr_login_server}/transactions-db:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "6001"
      }
      env {
        name  = "DB_PATH"
        value = "/app/data/transactions.db"
      }
      env {
        name  = "ANOMALIES_DB_URL"
        value = "http://anomalies-db/anomalies"
      }
      env {
        name  = "ANOMALIES_TIMEOUT_SECONDS"
        value = "10"
      }
      env {
        name  = "SAVINGS_DB_URL"
        value = "http://savings-db"
      }

      volume_mounts {
        name = "db-vol"
        path = "/app/data"
      }
    }
  }

  depends_on = [
    azurerm_container_app_environment_storage.db_storage
  ]
}
