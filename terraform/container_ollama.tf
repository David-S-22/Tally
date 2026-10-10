# ==============================================================================
# Container App: ollama
# Pulls raw image directly from Docker Hub — NO custom Dockerfile needed.
# Auto-pulls models on startup via inline shell command and persists to Azure Files.
# ==============================================================================

resource "azurerm_container_app" "ollama" {
  name                         = "ollama"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  workload_profile_name        = "ollama-profile"

  ingress {
    external_enabled           = false
    target_port                = 11434
    allow_insecure_connections = true

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  # No secret or registry block needed — pulls directly from public Docker Hub.

  template {
    min_replicas = var.ollama_min_replicas
    max_replicas = 1

    volume {
      name         = "ollama-vol"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.ollama_storage.name
    }

    container {
      name   = "ollama"
      image  = var.ollama_image
      cpu    = var.ollama_cpu
      memory = var.ollama_memory

      # Inline startup command replaces the custom Dockerfile entrypoint script.
      # Starts ollama serve in background, waits for daemon readiness, pulls all models
      # listed in OLLAMA_PULL_MODELS into the persistent volume, then waits on the daemon.
      command = ["/bin/sh", "-c"]
      args = [
        "ollama serve & pid=$! ; echo '[START] Waiting for ollama daemon...' ; until ollama list >/dev/null 2>&1; do sleep 1; done ; echo '[END] Waiting for ollama daemon.' ; echo '[START] Pulling models...' ; for m in $OLLAMA_PULL_MODELS; do echo \"[PULL] $m\" ; ollama pull \"$m\" ; done ; echo '[END] Pulling models.' ; wait $pid"
      ]

      env {
        name  = "OLLAMA_PULL_MODELS"
        value = var.ollama_models
      }

      volume_mounts {
        name = "ollama-vol"
        path = "/root/.ollama"
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 11434
        path                    = "/"
        interval_seconds        = 5
        timeout                 = 3
        failure_count_threshold = 10
        success_count_threshold = 1
      }
    }
  }

  depends_on = [
    azurerm_container_app_environment_storage.ollama_storage
  ]
}
