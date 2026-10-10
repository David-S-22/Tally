# Tally — Azure Container Apps Deployment (Terraform)

Terraform Infrastructure-as-Code (IaC) to deploy Tally to **Azure Container Apps** backed by **Azure Container Registry** and **Azure Files**.

## Architecture & Project Structure

The deployment is split into base infrastructure and **one standalone `.tf` script per container** for clarity, maintainability, and granular targeting.

```text
terraform/
├── main.tf                               # Provider, Resource Group, ACR, Log Analytics, Storage & CAE
├── variables.tf                          # Input variables with sensible cloud defaults (min_replicas = 0)
├── outputs.tf                            # FQDN URLs, ACR credentials (sensitive), and environment outputs
├── terraform.tfvars.example              # Template configuration file
│
├── container_ollama.tf                   # Dedicated compute profile, models volume, startup auto-pull (Docker Hub)
├── container_shared_frontend.tf          # Public-facing web UI / Reverse Proxy
│
├── container_transactions_frontend.tf    # Transactions Web UI
├── container_transactions_backend.tf     # Transactions API service
├── container_transactions_db.tf          # Transactions SQLite storage service
│
├── container_bills_frontend.tf           # Bills Web UI
├── container_bills_backend.tf            # Bills API service
├── container_bills_db.tf                 # Bills SQLite storage service
│
├── container_anomalies_frontend.tf       # Anomalies Web UI
├── container_anomalies_backend.tf        # Anomalies API service
├── container_anomalies_db.tf             # Anomalies SQLite storage service
│
├── container_budgets_frontend.tf         # Budgets Web UI
├── container_budgets_backend.tf          # Budgets API service
├── container_budgets_db.tf               # Budgets SQLite storage service
│
├── container_savings_frontend.tf         # Savings Web UI
├── container_savings_backend.tf          # Savings API service
└── container_savings_db.tf               # Savings SQLite storage service
```

---

## Infrastructure Summary

| Resource | Purpose |
|:---|:---|
| Resource Group | `ASD-GROUP-21` |
| Container Registry (ACR) | `tallyasd21` — hosts custom application container images |
| Log Analytics Workspace | Centralized logging for the environment |
| Storage Account + File Shares | Persistent volumes for Ollama models & SQLite databases |
| Container Apps Environment | Shared environment with Consumption + Dedicated profiles |
| 17 Container Apps | Dedicated app per microservice |

### Workload Profiles & Sizing

- **Scale to Zero (`min_replicas = 0`):** By default, all apps are configured with `min_replicas = 0`, allowing them to scale down to zero when idle to save compute costs.
- **Standard Microservices (16 containers):** Run on the serverless **Consumption** profile (0.5 vCPU / 1Gi RAM per container). Azure Container Apps enforces a 1:2 vCPU-to-memory ratio on Consumption.
- **Ollama LLM Engine:** Runs on a dedicated **`ollama-profile`** workload profile (`D8` node SKU = 8 vCPU / 32Gi) allocated 4 vCPU and 16Gi RAM to support LLM inference workloads.

### Persistent Volume Mounts (Azure Files)

| Volume Share | Size | Container Mount | App | Purpose |
|:---|:---|:---|:---|:---|
| `ollama-data` | 100 GB | `/root/.ollama` | `ollama` | Persists pulled LLM models across restarts |
| `db-data` | 10 GB | `/app/data` | `transactions-db` | `transactions.db` SQLite database |
| `db-data` | 10 GB | `/data` | `bills-db` | `bills.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `anomalies-db` | `anomalies.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `budgets-db` | `budgets.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `savings-db` | `savings.db` SQLite database |

---

## Key Azure Container Apps (ACA) Design Considerations

### 1. Service Discovery & Ports
- **Docker Compose:** Containers address each other via `http://<service>:<port>` directly on the container network.
- **Azure Container Apps:** Each container app registers an internal DNS name. The internal Envoy ingress proxy intercepts HTTP requests on port 80 and routes them to the container's configured `target_port`.
- **Rule:** All internal service URLs **must omit ports** (e.g. `http://transactions-backend`, `http://ollama`). Attempting to call `http://transactions-backend:5001` fails with connection refused because Envoy does not listen on port 5001.

### 2. Ollama Setup (No Custom Dockerfile Needed)
- Ollama uses the official **`ollama/ollama:latest`** image directly from public Docker Hub.
- **No Dockerfile or custom image building is required.**
- `container_ollama.tf` uses inline `command` and `args` to start `ollama serve` in the background, poll for readiness, pull all models in `OLLAMA_PULL_MODELS` into the persistent Azure Files mount (`/root/.ollama`), and wait on the server process.
- No ACR `secret` or `registry` block is attached to Ollama, avoiding registry lookup errors.

### 3. Strict Dependency Order
Terraform enforces strict provisioning order via explicit `depends_on` relationships:
```text
Storage Accounts & File Shares
       │
       ├──► Databases (transactions-db, bills-db, anomalies-db, budgets-db, savings-db)
       └──► Ollama
              │
              ▼
       Backends (transactions-backend, bills-backend, anomalies-backend, budgets-backend, savings-backend)
              │
              ▼
       Frontends (transactions-frontend, bills-frontend, anomalies-frontend, budgets-frontend, savings-frontend)
              │
              ▼
       Gateway (shared-frontend)
```
- Databases and Ollama are fully provisioned before backends start.
- Backends are fully provisioned before feature frontends start.
- Feature frontends are fully provisioned before `shared-frontend` starts.

---

## Deployment Guide

### 1. Azure Authentication
```bash
az login
az account set --subscription "55258ab7-e42a-4438-8174-f1777c67a393"
```

### 2. Build & Push Application Images to ACR
*(Note: Ollama is pulled directly from Docker Hub and does not need to be built or pushed)*
```bash
# Login to ACR (tallyasd21)
az acr login --name tallyasd21

# Build application microservices using docker compose
docker compose build

# Tag and push microservices to ACR
ACR=tallyasd21.azurecr.io
for s in shared-frontend \
         transactions-frontend transactions-backend transactions-db \
         bills-frontend bills-backend bills-db \
         anomalies-frontend anomalies-backend anomalies-db \
         budgets-frontend budgets-backend budgets-db \
         savings-frontend savings-backend savings-db; do
  docker tag "tally-$s:latest" "$ACR/$s:latest" 2>/dev/null \
    || docker tag "$s:latest" "$ACR/$s:latest" 2>/dev/null
  docker push "$ACR/$s:latest"
done
```

### 3. Deploy via Terraform
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars  # customize if needed
terraform init
terraform plan
terraform apply
```

### 4. Access Application
```bash
terraform output application_url
```

### 5. Retrieve ACR Admin Password (if needed for docker login)
```bash
terraform output -raw acr_admin_password
```

### Teardown
```bash
terraform destroy
```
