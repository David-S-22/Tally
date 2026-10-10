output "acr_login_server" {
  description = "The login server for the Azure Container Registry"
  value       = azurerm_container_registry.acr.login_server
}

output "acr_admin_username" {
  description = "Admin username for the Azure Container Registry"
  value       = azurerm_container_registry.acr.admin_username
}

output "acr_admin_password" {
  description = "Admin password for the Azure Container Registry (sensitive)"
  value       = azurerm_container_registry.acr.admin_password
  sensitive   = true
}

output "application_url" {
  description = "Public URL to access the Tally shared frontend"
  value       = "https://${azurerm_container_app.shared_frontend.latest_revision_fqdn}"
}

output "environment_default_domain" {
  description = "Default domain for the Azure Container Apps environment"
  value       = azurerm_container_app_environment.env.default_domain
}

output "ollama_profile" {
  description = "Workload profile assigned to Ollama"
  value = {
    profile_type = var.ollama_workload_profile_type
    cpu          = var.ollama_cpu
    memory       = var.ollama_memory
    image        = azurerm_container_app.ollama.template[0].container[0].image
  }
}
