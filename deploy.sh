#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ACR_NAME="tallyasd21"
RESOURCE_GROUP="ASD-GROUP-21"
LOGIN_SERVER="${ACR_NAME}.azurecr.io"
VM_NAME="vm-tally-ollama"
AI_VM_NAME="vm-tally-ai"
AI_DNS_NAME="tally-ai.australiaeast.cloudapp.azure.com"
DNS_NAME="tally-ollama.australiaeast.cloudapp.azure.com"
OLLAMA_PORT="11434"
OLLAMA_URL="http://${DNS_NAME}:${OLLAMA_PORT}"

check_ollama_health() {
    local response
    response=$(curl -s -f --max-time 3 "${OLLAMA_URL}/api/tags" 2>/dev/null) || return 1
    echo "$response" | grep -q '"models"' || return 1
    return 0
}

wait_for_ollama_ready() {
    local timeout_secs="${1:-300}"
    local interval=5
    local elapsed=0

    echo "==> Running health check on Ollama VM (${OLLAMA_URL}/api/tags)..."
    while [ $elapsed -lt $timeout_secs ]; do
        if check_ollama_health; then
            local model_count
            model_count=$(curl -s "${OLLAMA_URL}/api/tags" 2>/dev/null | grep -o '"name":' | wc -l)
            if [ "$model_count" -ge 1 ]; then
                echo "==> [HEALTHY] Ollama VM is ready! ($model_count model(s) available)"
                return 0
            else
                echo "--> Ollama service is up, waiting for models to finish downloading (${elapsed}s)..."
            fi
        else
            echo "--> Waiting for Ollama VM endpoint to come online (${elapsed}s / max ${timeout_secs}s)..."
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
    done

    echo "WARNING: Timed out waiting for Ollama health check (${timeout_secs}s)."
    echo "Containers may experience temporary AI errors while models finish downloading."
    return 1
}

show_health() {
    echo "==> Checking Ollama VM health at ${OLLAMA_URL}..."
    if check_ollama_health; then
        echo "[HEALTHY] Ollama VM is online and responding."
        echo "Available Models:"
        curl -s "${OLLAMA_URL}/api/tags" | jq -r '.models[]? | "  - \(.name)"' 2>/dev/null || curl -s "${OLLAMA_URL}/api/tags"
    else
        echo "[UNAVAILABLE] Ollama VM is currently unreachable or offline."
        echo "To start the VM: az vm start --resource-group $RESOURCE_GROUP --name $VM_NAME"
    fi
}

SERVICES=(
    "transactions-db:janelle/database/Dockerfile:."
    "transactions-backend:janelle/backend/Dockerfile:."
    "transactions-frontend:janelle/frontend/Dockerfile:janelle/frontend"
    "bills-db:sophia/database/Dockerfile:sophia/database"
    "bills-backend:sophia/backend/Dockerfile:sophia/backend"
    "bills-frontend:sophia/frontend/Dockerfile:sophia/frontend"
    "anomalies-db:aiden/database/Dockerfile:."
    "anomalies-backend:aiden/backend/Dockerfile:."
    "anomalies-frontend:aiden/frontend/Dockerfile:aiden/frontend"
    "budgets-db:ethan/database/Dockerfile:ethan/database"
    "budgets-backend:ethan/backend/Dockerfile:ethan/backend"
    "budgets-frontend:ethan/frontend/Dockerfile:ethan/frontend"
    "savings-db:david/database/Dockerfile:."
    "savings-backend:david/backend/Dockerfile:."
    "savings-frontend:david/frontend/Dockerfile:david/frontend"
    "shared-frontend:shared/frontend/Dockerfile:shared/frontend"
)

get_image_tag() {
    local tag
    tag=$(git rev-parse --short HEAD 2>/dev/null || date +%s)
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        tag="${tag}-$(date +%s)"
    fi
    echo "$tag"
}

build_and_push_containers() {
    local tag="$1"

    # Ensure ACR exists before logging in (enables bootstrapping on fresh resource groups)
    if ! az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
        echo "==> ACR '$ACR_NAME' not found in '$RESOURCE_GROUP'. Creating registry..."
        az acr create \
            --name "$ACR_NAME" \
            --resource-group "$RESOURCE_GROUP" \
            --location "australiaeast" \
            --sku Standard \
            --admin-enabled true \
            --output none
    fi

    echo "==> Logging in to Azure Container Registry..."
    az acr login --name "$ACR_NAME"

    local use_buildx=false
    if docker buildx version &>/dev/null; then
        use_buildx=true
    fi

    echo "==> Building and pushing microservice containers (tag: $tag, buildx cache: $use_buildx)..."
    for item in "${SERVICES[@]}"; do
        IFS=':' read -r name dockerfile ctx <<< "$item"
        echo "--> Building $name ($tag)..."
        if [ "$use_buildx" = true ]; then
            docker buildx build \
                --platform linux/amd64 \
                --cache-from "type=registry,ref=$LOGIN_SERVER/$name:latest" \
                --cache-to "type=inline" \
                -t "$LOGIN_SERVER/$name:$tag" \
                -t "$LOGIN_SERVER/$name:latest" \
                -f "$dockerfile" "$ctx" \
                --push
        else
            docker build --platform linux/amd64 -t "$LOGIN_SERVER/$name:$tag" -t "$LOGIN_SERVER/$name:latest" -f "$dockerfile" "$ctx"
            echo "--> Pushing $name..."
            docker push "$LOGIN_SERVER/$name:$tag"
            docker push "$LOGIN_SERVER/$name:latest"
        fi
    done
}

# Update existing apps in batches of four; wait for each batch before continuing.
update_container_apps() {
    local tag="$1"
    local offset item name index failed
    local -a pids names
    for ((offset = 0; offset < ${#SERVICES[@]}; offset += 4)); do
        pids=()
        names=()
        failed=0
        for item in "${SERVICES[@]:offset:4}"; do
            name="${item%%:*}"
            echo "--> Updating $name..."
            az containerapp update \
                --name "$name" \
                --resource-group "$RESOURCE_GROUP" \
                --image "$LOGIN_SERVER/$name:$tag" \
                --output none &
            pids+=("$!")
            names+=("$name")
        done
        # Check every job so a failure does not leave sibling updates unwaited.
        for index in "${!pids[@]}"; do
            if wait "${pids[$index]}"; then
                echo "--> Updated ${names[$index]}"
            else
                echo "ERROR: Update failed for ${names[$index]}" >&2
                failed=1
            fi
        done
        [ "$failed" -eq 0 ] || return 1
    done
}

# Native Python services: provision once, then pull code only on deployment.
provision_ai_vms() {
    : "${MULTI_AGENT_TOKEN:?Set MULTI_AGENT_TOKEN before provisioning}"
    [[ "$MULTI_AGENT_TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || { echo 'Use a token of 32+ letters, digits, underscores or hyphens.' >&2; return 1; }
    export CONTAINER_DOMAIN
    CONTAINER_DOMAIN=$(az containerapp env show -g "$RESOURCE_GROUP" -n cae-tally --query properties.defaultDomain -o tsv)
    export AI_VM_SSH_PUBLIC_KEY="${AI_VM_SSH_PUBLIC_KEY:-$(cat "$HOME/.ssh/id_rsa.pub")}"
    az deployment group create -g "$RESOURCE_GROUP" --name tally-ai-vms \
        --template-file bicep/ai-services.bicep --parameters bicep/ai-services.bicepparam --output none
    for service in rag mcp multi-agent; do check_ai_endpoint "$service"; done
}

update_ai_services_vm() {
    local services=("$@")
    local service result
    [ "${#services[@]}" -gt 0 ] || services=(rag mcp multi-agent)
    for service in "${services[@]}"; do
        case "$service" in mcp|rag|multi-agent) ;; *) echo "Unknown service: $service" >&2; return 1 ;; esac
        az vm start -g "$RESOURCE_GROUP" -n "$AI_VM_NAME" --output none
        # Azure Run Command can return success even when the script fails.
        result=$(az vm run-command invoke -g "$RESOURCE_GROUP" -n "$AI_VM_NAME" \
            --command-id RunShellScript \
            --scripts "/usr/local/bin/tally-ai-service $service && echo TALLY_UPDATE_OK" \
            --query 'value[0].message' -o tsv)
        printf '%s\n' "$result"
        [[ "$result" == *TALLY_UPDATE_OK* ]] || return 1
        check_ai_endpoint "$service"
    done
}

check_ai_endpoint() {
    local service="$1" endpoint expected code
    case "$service" in
        mcp) endpoint=8000/mcp; expected='^(200|400|406)$' ;;
        rag) endpoint=5003/health; expected='^200$' ;;
        multi-agent) endpoint=5010/health; expected='^401$' ;; # Requires authentication.
    esac
    code=$(curl -sS --retry 12 --retry-connrefused --retry-delay 5 --max-time 30 \
        -o /dev/null -w '%{http_code}' "http://$AI_DNS_NAME:$endpoint")
    [[ "$code" =~ $expected ]] || { echo "$service endpoint failed: HTTP $code" >&2; return 1; }
}

TARGET="${1:-all}"
case "$TARGET" in
    ai-vms|--ai-vms) provision_ai_vms; exit 0 ;;
    ai-update|--ai-update)
        shift
        update_ai_services_vm "$@"
        exit 0
        ;;
    --health|health)
        show_health
        exit 0
        ;;
    --update|update)
        FAST_UPDATE=true
        DEPLOY_CONTAINERS=false
        DEPLOY_VM=false
        ;;
    --containers|containers)
        FAST_UPDATE=false
        DEPLOY_CONTAINERS=true
        DEPLOY_VM=false
        ;;
    --vm|vm)
        FAST_UPDATE=false
        DEPLOY_CONTAINERS=false
        DEPLOY_VM=true
        ;;
    --all|all|"")
        FAST_UPDATE=false
        DEPLOY_CONTAINERS=true
        DEPLOY_VM=true
        ;;
    *)
        echo "Usage: ./deploy.sh [all | containers | update | vm | health | ai-vms | ai-update]"
        echo ""
        echo "Options:"
        echo "  all         Deploy Ollama, containers and the shared AI VM (default)"
        echo "  containers  Deploy microservice containers via Bicep (skip VM)"
        echo "  update      Fast update: rebuild, push, then update apps four at a time (skips Bicep)"
        echo "  vm          Deploy Ollama VM only and wait for health check"
        echo "  ai-vms      Provision the shared VM for all three Python services"
        echo "  ai-update   Update Python services; optionally append mcp, rag, multi-agent"
        echo "  health      Check current health and models of the Ollama VM"
        exit 1
        ;;
esac

# Validate required configuration before any infrastructure changes.
if [ "$DEPLOY_CONTAINERS" = true ]; then
    : "${MULTI_AGENT_TOKEN:?Set MULTI_AGENT_TOKEN before deploying backend configuration}"
fi

# --- Pre-flight Health Notice for Containers ---
if [ "$DEPLOY_CONTAINERS" = true ] && [ "$DEPLOY_VM" = false ]; then
    echo "==> Checking if Ollama VM is currently accessible..."
    if check_ollama_health; then
        echo "==> Ollama VM is online and healthy."
    else
        echo "WARNING: Ollama VM is currently unreachable."
        echo "Container apps will deploy, but AI calls will fail until the VM is started."
        echo "To start the VM: az vm start --resource-group $RESOURCE_GROUP --name $VM_NAME"
    fi
fi

# --- Step 1: Provision VM if requested ---
if [ "$DEPLOY_VM" = true ]; then
    echo "==> 1. Provisioning Ollama VM via Bicep (Standard_B4as_v2 / australiaeast)..."
    PARAMS=()
    if [ -f "$HOME/.ssh/id_rsa.pub" ]; then
        PARAMS+=("--parameters" "adminPublicKey=$(cat "$HOME/.ssh/id_rsa.pub")")
    fi
    az deployment group create \
        --resource-group "$RESOURCE_GROUP" \
        --template-file bicep/modules/ollama-vm.bicep \
        "${PARAMS[@]}" \
        --rollback-on-error

    # Health check: Wait for VM to be ready before containers try to access it
    wait_for_ollama_ready 300 || true
fi

# --- Fast Update: Directly update Container Apps without running Bicep ---
if [ "$FAST_UPDATE" = true ]; then
    IMAGE_TAG=$(get_image_tag)
    echo "==> Using image tag: $IMAGE_TAG"

    build_and_push_containers "$IMAGE_TAG"

    echo "==> Updating Container Apps in Azure via CLI..."
    update_container_apps "$IMAGE_TAG"

    echo "==> Fast update completed successfully!"
    exit 0
fi

# --- Step 2: Build, Push & Deploy Containers via Bicep ---
if [ "$DEPLOY_CONTAINERS" = true ]; then
    IMAGE_TAG=$(get_image_tag)
    echo "==> Using image tag: $IMAGE_TAG"

    build_and_push_containers "$IMAGE_TAG"

    # --- Step 3: Deploy Container Apps ---
    echo "==> Deploying Container Apps to Azure (imageTag: $IMAGE_TAG)..."
    az deployment group create \
        --resource-group "$RESOURCE_GROUP" \
        --template-file bicep/main.bicep \
        --parameters bicep/main.bicepparam \
        --parameters deployOllamaVm=false \
        --parameters imageTag="$IMAGE_TAG" \
        --rollback-on-error
fi

if [ "$TARGET" = all ] || [ "$TARGET" = --all ]; then
    provision_ai_vms
fi

echo "==> Deployment completed successfully!"
