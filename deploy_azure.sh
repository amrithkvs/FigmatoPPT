#!/usr/bin/env bash
set -euo pipefail

# Azure deployment for Figma Deck
# Prereqs:
#   1. az login
#   2. az account set --subscription <subscription-id-or-name>  (optional if default is correct)
# Usage:
#   chmod +x deploy_azure.sh
#   ./deploy_azure.sh
# Optional overrides:
#   AZ_LOCATION=eastus
#   AZ_RESOURCE_GROUP=figma-deck-rg
#   AZ_APP_NAME=figma-deck-app-<suffix>
#   AZ_PLAN_NAME=figma-deck-plan
#   AZ_ACR_NAME=figmadeckacr<suffix>
#   AZ_STORAGE_NAME=figmadeckst<suffix>
#   AZ_SHARE_NAME=figmadeckdata
#   AZ_IMAGE_NAME=figma-deck:latest

suffix_default=$(date +%m%d%H%M)
AZ_LOCATION=${AZ_LOCATION:-eastus}
AZ_RESOURCE_GROUP=${AZ_RESOURCE_GROUP:-figma-deck-rg}
AZ_PLAN_NAME=${AZ_PLAN_NAME:-figma-deck-plan}
AZ_APP_NAME=${AZ_APP_NAME:-figma-deck-app-${suffix_default}}
AZ_ACR_NAME=${AZ_ACR_NAME:-figmadeckacr${suffix_default}}
AZ_STORAGE_NAME=${AZ_STORAGE_NAME:-figmadeckst${suffix_default}}
AZ_SHARE_NAME=${AZ_SHARE_NAME:-figmadeckdata}
AZ_IMAGE_NAME=${AZ_IMAGE_NAME:-figma-deck:latest}
WEBHOOK_BASE_URL=${WEBHOOK_BASE_URL:-https://${AZ_APP_NAME}.azurewebsites.net}
FIGMA_DECK_DB_PATH=${FIGMA_DECK_DB_PATH:-/data/decks/data.db}
DECKS_OUTPUT_DIR=${DECKS_OUTPUT_DIR:-/data/decks}

repo_root=$(cd "$(dirname "$0")" && pwd)

# Resource names must be lowercase and storage/acr names must be globally unique.
AZ_ACR_NAME=$(echo "$AZ_ACR_NAME" | tr '[:upper:]' '[:lower:]')
AZ_STORAGE_NAME=$(echo "$AZ_STORAGE_NAME" | tr '[:upper:]' '[:lower:]')
AZ_APP_NAME=$(echo "$AZ_APP_NAME" | tr '[:upper:]' '[:lower:]')

if [[ ${#AZ_STORAGE_NAME} -gt 24 ]]; then
  echo "AZ_STORAGE_NAME must be <= 24 chars: $AZ_STORAGE_NAME" >&2
  exit 1
fi

if [[ ${#AZ_ACR_NAME} -gt 50 ]]; then
  echo "AZ_ACR_NAME must be <= 50 chars: $AZ_ACR_NAME" >&2
  exit 1
fi

if [[ ${#AZ_APP_NAME} -gt 60 ]]; then
  echo "AZ_APP_NAME should be <= 60 chars for sanity: $AZ_APP_NAME" >&2
  exit 1
fi

echo "==> Using Azure resources"
echo "Resource Group : $AZ_RESOURCE_GROUP"
echo "Location       : $AZ_LOCATION"
echo "App Service    : $AZ_APP_NAME"
echo "ACR            : $AZ_ACR_NAME"
echo "Storage        : $AZ_STORAGE_NAME"
echo "File share     : $AZ_SHARE_NAME"
echo "Image          : $AZ_IMAGE_NAME"

echo "==> Creating resource group"
az group create --name "$AZ_RESOURCE_GROUP" --location "$AZ_LOCATION" >/dev/null

echo "==> Creating container registry"
az acr create \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACR_NAME" \
  --sku Basic \
  --admin-enabled true >/dev/null

echo "==> Building and pushing container image in ACR"
az acr build \
  --registry "$AZ_ACR_NAME" \
  --image "$AZ_IMAGE_NAME" \
  "$repo_root"

echo "==> Creating storage account"
az storage account create \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_STORAGE_NAME" \
  --location "$AZ_LOCATION" \
  --sku Standard_LRS >/dev/null

echo "==> Creating Azure Files share"
az storage share-rm create \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --storage-account "$AZ_STORAGE_NAME" \
  --name "$AZ_SHARE_NAME" >/dev/null

echo "==> Creating Linux App Service plan"
az appservice plan create \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_PLAN_NAME" \
  --is-linux \
  --sku B1 >/dev/null

echo "==> Creating web app"
az webapp create \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --plan "$AZ_PLAN_NAME" \
  --name "$AZ_APP_NAME" \
  --deployment-container-image-name "$AZ_ACR_NAME.azurecr.io/$AZ_IMAGE_NAME" >/dev/null

echo "==> Configuring container registry settings"
ACR_USER=$(az acr credential show --name "$AZ_ACR_NAME" --query username -o tsv)
ACR_PASS=$(az acr credential show --name "$AZ_ACR_NAME" --query passwords[0].value -o tsv)

az webapp config container set \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APP_NAME" \
  --container-image-name "$AZ_ACR_NAME.azurecr.io/$AZ_IMAGE_NAME" \
  --container-registry-url "https://$AZ_ACR_NAME.azurecr.io" \
  --container-registry-user "$ACR_USER" \
  --container-registry-password "$ACR_PASS" >/dev/null

echo "==> Mounting Azure Files share at /data"
STORAGE_KEY=$(az storage account keys list \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --account-name "$AZ_STORAGE_NAME" \
  --query '[0].value' -o tsv)

az webapp config storage-account add \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APP_NAME" \
  --custom-id "$AZ_SHARE_NAME" \
  --storage-type AzureFiles \
  --share-name "$AZ_SHARE_NAME" \
  --account-name "$AZ_STORAGE_NAME" \
  --access-key "$STORAGE_KEY" \
  --mount-path /data >/dev/null

echo "==> Setting app settings"
az webapp config appsettings set \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APP_NAME" \
  --settings \
    WEBSITES_PORT=8000 \
    DECKS_OUTPUT_DIR="$DECKS_OUTPUT_DIR" \
    FIGMA_DECK_DB_PATH="$FIGMA_DECK_DB_PATH" \
    WEBHOOK_BASE_URL="$WEBHOOK_BASE_URL" >/dev/null

echo "==> Restarting app"
az webapp restart --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_APP_NAME" >/dev/null

echo "==> Deployment complete"
echo "App URL: https://${AZ_APP_NAME}.azurewebsites.net"
echo "Webhook base URL: ${WEBHOOK_BASE_URL}"
