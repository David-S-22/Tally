# Tally — Azure Container Apps Deployment (Bicep)

Azure Bicep Infrastructure-as-Code (IaC) to deploy Tally to **Azure Container Apps** backed by **Azure Container Registry** and **Azure Files**.

## Architecture & Project Structure

The deployment is split into base infrastructure orchestration and **one standalone module per container** in `modules/` for clarity, maintainability, and granular targeting.

```text
bicep/
├── main.bicep                               # Resource Group scoped template (ACR, Log Analytics, Storage, CAE & Container modules)
├── subscription.bicep                       # Subscription scoped template (creates Resource Group and calls main.bicep)
├── main.bicepparam                          # Bicep parameters configuration file
├── deploy.sh                                # Automated build, push, and deployment script
│
├── modules/
│   ├── container_ollama.bicep               # Dedicated compute profile, models volume, startup auto-pull (Docker Hub)
│   ├── container_shared_frontend.bicep      # Public-facing web UI / Reverse Proxy gateway
│   │
│   ├── container_transactions_frontend.bicep# Transactions Web UI
│   ├── container_transactions_backend.bicep # Transactions API service
│   ├── container_transactions_db.bicep      # Transactions SQLite storage service
│   │
│   ├── container_bills_frontend.bicep       # Bills Web UI
│   ├── container_bills_backend.bicep        # Bills API service
│   ├── container_bills_db.bicep             # Bills SQLite storage service
│   │
│   ├── container_anomalies_frontend.bicep   # Anomalies Web UI
│   ├── container_anomalies_backend.bicep    # Anomalies API service
│   ├── container_anomalies_db.bicep         # Anomalies SQLite storage service
│   │
│   ├── container_budgets_frontend.bicep     # Budgets Web UI
│   ├── container_budgets_backend.bicep      # Budgets API service
│   ├── container_budgets_db.bicep           # Budgets SQLite storage service
│   │
│   ├── container_savings_frontend.bicep     # Savings Web UI
│   ├── container_savings_backend.bicep      # Savings API service
│   └── container_savings_db.bicep           # Savings SQLite storage service
```

---

## Infrastructure Summary

| Resource | Purpose |
|:---|:---|
| Resource Group | `ASD-GROUP-21` |
| Container Registry (ACR) | `tallyasd21` — hosts custom application container images |
| Log Analytics Workspace | Centralized logging for the environment (`log-cae-tally`) |
| Storage Account + File Shares | Persistent Azure Files shares for Ollama models & SQLite databases |
| Container Apps Environment | Shared environment (`cae-tally`) with Consumption + Dedicated profiles |
| 17 Container Apps | Dedicated microservice containers |

### Workload Profiles & Sizing

- **Scale to Zero (`minReplicas = 0`):** All containers (including Ollama and all 16 microservices) are configured with `minReplicas = 0`, scaling down to zero when idle to conserve cloud spend.
- **Standard Microservices (16 containers):** Run on the serverless **Consumption** profile allocated **2.0 vCPU and 4Gi RAM** per container.
- **Ollama LLM Engine:** Runs on a dedicated **`ollama-profile`** workload profile (**`D8`** node SKU) allocated **4.0 vCPU and 16Gi RAM**, scaling between 0 and 2 dedicated nodes. Persistent model weights are stored on Azure Files so model data is preserved across scale-to-zero events.

### Persistent Volume Mounts (Azure Files)

| Volume Share | Size | Container Mount | App | Purpose |
|:---|:---|:---|:---|:---|
| `ollama-data` | 100 GB | `/root/.ollama` | `ollama` | Persists pulled LLM models across restarts |
| `db-data` | 10 GB | `/app/data` | `transactions-db` | `transactions.db` SQLite database |
| `db-data` | 10 GB | `/data` | `bills-db` | `bills.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `anomalies-db` | `anomalies.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `budgets-db` | `budgets.db` SQLite database |
| `db-data` | 10 GB | `/app/data` | `savings-db` | `savings.db` SQLite database |

> [!WARNING]
> **SQLite on Azure Files (SMB):** Azure Files SMB shares lack POSIX byte-range locking. All database containers are strictly capped at `maxReplicas = 1` to prevent database corruption.

---

## Key Azure Container Apps (ACA) Design Considerations

### 1. Service Discovery & Ports
- **Docker Compose:** Containers address each other via `http://<service>:<port>` directly on the container network.
- **Azure Container Apps:** Each container app registers an internal DNS name. The internal Envoy ingress proxy intercepts HTTP requests on port 80 and routes them to the container's configured `targetPort`.
- **Rule:** All internal service URLs **must omit ports** (e.g. `http://transactions-backend`, `http://ollama`).

### 2. Reverse Proxy & Host Headers
- In Azure Container Apps, Envoy routes incoming requests based on the HTTP `Host` header.
- In NGINX reverse proxies (such as `shared-frontend`), ensure internal proxies rewrite the Host header to match the destination container (e.g. `$proxy_host`).

### 3. Ollama Setup (No Custom Dockerfile Needed)
- Ollama uses the official **`ollama/ollama:latest`** image directly from public Docker Hub.
- `container_ollama.bicep` uses inline `command` and `args` to start `ollama serve` in the background, poll for readiness, pull all models in `OLLAMA_PULL_MODELS` into the persistent Azure Files mount (`/root/.ollama`), and wait on the server process.
- Includes an HTTP `probes` readiness check on port 11434 to prevent traffic routing before Ollama starts serving.

### 4. Strict Dependency Order
Bicep enforces strict provisioning order via module dependencies:
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

---

## Deployment Guide

### Option A: One-Command Automated Deployment (Recommended)

Run the included automated deployment script:
```bash
cd bicep
./deploy.sh
```
This script handles:
1. Validating Azure authentication.
2. Creating the Resource Group (`ASD-GROUP-21`) if needed.
3. Authenticating to Azure Container Registry (`tallyasd21`).
4. Building microservices with `docker compose build`.
5. Tagging and pushing images to ACR.
6. Executing `az deployment group create --template-file main.bicep --parameters main.bicepparam`.

---

### Option B: Manual Step-by-Step Deployment

#### 1. Azure Authentication
```bash
az login
az account set --subscription "55258ab7-e42a-4438-8174-f1777c67a393"
```

#### 2. Create Resource Group (if not already created)
```bash
az group create --name ASD-GROUP-21 --location australiaeast
```

#### 3. Build & Push Application Images to ACR
```bash
# Login to ACR (tallyasd21)
az acr login --name tallyasd21

# Build application microservices using docker compose from the repo root
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

#### 4. Deploy via Azure CLI (Bicep)

Deploy using the parameter file (`main.bicepparam`):
```bash
cd bicep
az deployment group create \
  --resource-group ASD-GROUP-21 \
  --template-file main.bicep \
  --parameters main.bicepparam
```

Or deploy at the subscription scope (creating the resource group automatically):
```bash
az deployment sub create \
  --location australiaeast \
  --template-file subscription.bicep \
  --parameters resourceGroupName=ASD-GROUP-21 location=australiaeast
```

#### 5. Access Application
Retrieve the application URL from deployment outputs:
```bash
az deployment group show \
  --resource-group ASD-GROUP-21 \
  --name main \
  --query properties.outputs.applicationUrl.value -o tsv
```

### Teardown
To delete the entire deployment and all resources:
```bash
az group delete --name ASD-GROUP-21 --yes --no-wait
```
