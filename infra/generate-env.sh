#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --resource-group RESOURCE_GROUP --deployment DEPLOYMENT [--output FILE]" >&2
}

resource_group=""
deployment=""
output=".env.azure"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resource-group) resource_group="$2"; shift 2 ;;
    --deployment) deployment="$2"; shift 2 ;;
    --output) output="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "$resource_group" && -n "$deployment" ]] || { usage; exit 2; }

output_dir="$(dirname "$output")"
mkdir -p "$output_dir"
get_output() {
  az deployment group show -g "$resource_group" -n "$deployment" \
    --query "properties.outputs.$1.value" -o tsv
}

search_name="$(get_output searchName)"
search_endpoint="$(get_output searchEndpoint)"
storage_name="$(get_output storageName)"
vi_name="$(get_output videoIndexerName)"
vi_account_id="$(get_output videoIndexerAccountId)"
subscription_id="$(az account show --query id -o tsv)"
search_key="$(az search admin-key show -g "$resource_group" --service-name "$search_name" --query primaryKey -o tsv)"
storage_key="$(az storage account keys list -g "$resource_group" -n "$storage_name" --query '[0].value' -o tsv)"
location="$(az group show -g "$resource_group" --query location -o tsv)"
index_name="$(get_output searchIndexName)"
app_insights_connection_string="$(get_output appInsightsConnectionString)"

umask 077
cat > "$output" <<EOF
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=${GROQ_API_KEY:-}
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
AZURE_SEARCH_ENDPOINT=${search_endpoint}
AZURE_SEARCH_API_KEY=${search_key}
AZURE_SEARCH_INDEX_NAME=${index_name}
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=${storage_name};AccountKey=${storage_key};EndpointSuffix=core.windows.net
AZURE_VI_NAME=${vi_name}
AZURE_VI_LOCATION=${location}
AZURE_VI_ACCOUNT_ID=${vi_account_id}
AZURE_SUBSCRIPTION_ID=${subscription_id}
AZURE_RESOURCE_GROUP=${resource_group}
APPLICATIONINSIGHTS_CONNECTION_STRING=${app_insights_connection_string}
LANGSMITH_TRACING=${LANGSMITH_TRACING:-false}
LANGSMITH_ENDPOINT=${LANGSMITH_ENDPOINT:-https://api.smith.langchain.com}
LANGSMITH_API_KEY=${LANGSMITH_API_KEY:-}
LANGSMITH_PROJECT=${LANGSMITH_PROJECT:-policy-lens-ai}
LANGCHAIN_TRACING_V2=${LANGCHAIN_TRACING_V2:-${LANGSMITH_TRACING:-false}}
LANGCHAIN_ENDPOINT=${LANGCHAIN_ENDPOINT:-${LANGSMITH_ENDPOINT:-https://api.smith.langchain.com}}
LANGCHAIN_API_KEY=${LANGCHAIN_API_KEY:-${LANGSMITH_API_KEY:-}}
LANGCHAIN_PROJECT=${LANGCHAIN_PROJECT:-${LANGSMITH_PROJECT:-policy-lens-ai}}
EOF
