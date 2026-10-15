#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./infra/deploy.sh -g RESOURCE_GROUP [-l LOCATION] [-p PROJECT_NAME]

Required tool: az
The Azure CLI must be logged in and the selected subscription must be set.
EOF
}

resource_group=""
location=""
project_name="policylens"

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

while getopts ":g:l:p:h" opt; do
  case "$opt" in
    g) resource_group="$OPTARG" ;;
    l) location="$OPTARG" ;;
    p) project_name="$OPTARG" ;;
    h) usage; exit 0 ;;
    \?|:) usage >&2; exit 2 ;;
  esac
done

[[ -n "$resource_group" ]] || { usage >&2; exit 2; }
command -v az >/dev/null || { echo "Azure CLI (az) is required." >&2; exit 1; }

location="${location:-$(az group show --name "$resource_group" --query location -o tsv 2>/dev/null || true)}"
if [[ -z "$location" ]]; then
  echo "Resource group does not exist; provide -l LOCATION so it can be created." >&2
  exit 1
fi

az group create --name "$resource_group" --location "$location" --output none

video_indexer_principal_id="${VIDEO_INDEXER_PRINCIPAL_ID:-}"
if [[ -z "$video_indexer_principal_id" ]]; then
  if [[ -n "${AZURE_CLIENT_ID:-}" ]]; then
    video_indexer_principal_id="$(az ad sp show --id "$AZURE_CLIENT_ID" --query id -o tsv 2>/dev/null || true)"
  fi
fi
if [[ -z "$video_indexer_principal_id" ]]; then
  video_indexer_principal_id="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
fi
if [[ -z "$video_indexer_principal_id" ]]; then
  signed_in_identity="$(az account show --query user.name -o tsv)"
  video_indexer_principal_id="$(az ad sp show --id "$signed_in_identity" --query id -o tsv 2>/dev/null || true)"
fi
if [[ -z "$video_indexer_principal_id" ]]; then
  echo "Could not determine the signed-in Azure identity object ID." >&2
  echo "Set VIDEO_INDEXER_PRINCIPAL_ID to the identity object ID and retry." >&2
  exit 1
fi

deployment_name="policylens-$(date +%Y%m%d%H%M%S)"
az deployment group create \
  --resource-group "$resource_group" \
  --name "$deployment_name" \
  --template-file infra/main.bicep \
  --parameters location="$location" \
  --parameters projectName="$project_name" \
  --parameters videoIndexerPrincipalId="$video_indexer_principal_id" \
  --output none

./infra/generate-env.sh \
  --resource-group "$resource_group" \
  --deployment "$deployment_name" \
  --output .env.azure

echo "Azure resources created."
echo "Runtime configuration written to .env.azure (keep it private)."
