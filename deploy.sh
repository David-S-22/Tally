#!/usr/bin/env bash
set -e

ACR_NAME="tallyasd21"
RESOURCE_GROUP="ASD-GROUP-21"
LOGIN_SERVER="${ACR_NAME}.azurecr.io"

echo "==> 1. Logging in to Azure Container Registry..."
az acr login --name "$ACR_NAME"

echo "==> 2. Building and pushing microservice containers..."
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

for item in "${SERVICES[@]}"; do
    IFS=':' read -r name dockerfile ctx <<< "$item"
    echo "--> Building $name..."
    docker build --platform linux/amd64 -t "$LOGIN_SERVER/$name:latest" -f "$dockerfile" "$ctx"
    echo "--> Pushing $name..."
    docker push "$LOGIN_SERVER/$name:latest"
done

echo "==> 3. Deploying Bicep template to Azure Container Apps..."
az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file bicep/main.bicep \
    --parameters bicep/main.bicepparam \
    --rollback-on-error

echo "==> Deployment completed successfully!"
