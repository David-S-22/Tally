# ==============================================================================
# Container App: savings-db
# ==============================================================================

resource "azurerm_container_app" "savings_db" {
  name                         = "savings-db"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  ingress {
    external_enabled           = false
    target_port                = 6002
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
      name   = "savings-db"
      image  = "${local.acr_login_server}/savings-db:${var.image_tag}"
      cpu    = var.default_cpu
      memory = var.default_memory

      env {
        name  = "FLASK_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "6002"
      }
      env {
        name  = "DB_PATH"
        value = "/app/data/savings.db"
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
