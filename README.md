# PolicyLens AI - Compliance QA Pipeline

PolicyLens AI is an AI-powered compliance auditing pipeline that analyzes video content against policy and regulatory guidance.

It combines:
- Video ingestion and indexing (YouTube -> Azure Video Indexer)
- Knowledge retrieval (Azure AI Search vector index of policy PDFs)
- LLM-based compliance reasoning (Groq + RAG)
- Workflow orchestration (LangGraph)
- API serving and telemetry (FastAPI + Azure Monitor OpenTelemetry)

## What This Project Does

Given a video URL, the system:
1. Downloads the video.
2. Uploads it to Azure Video Indexer and waits for processing.
3. Extracts transcript and OCR text.
4. Retrieves the most relevant policy chunks from Azure AI Search.
5. Uses Groq to detect violations and generate a final compliance report.
6. Returns structured results (`PASS` or `FAIL`, violations, and summary report).

## Architecture

Execution graph:

`START -> indexer -> auditor -> END`

- `indexer` node:
	- Downloads YouTube content.
	- Uploads to Azure Video Indexer.
	- Extracts transcript, OCR, and metadata.
- `auditor` node:
	- Runs vector retrieval from Azure AI Search.
	- Calls Groq with strict JSON output instructions.
	- Produces final compliance status and report.

Main implementation files:
- `main.py` - CLI simulation entry point.
- `backend/src/api/server.py` - FastAPI application (`/audit`, `/health`).
- `backend/src/graph/workflow.py` - LangGraph DAG wiring.
- `backend/src/graph/nodes.py` - Indexer and auditor nodes.
- `backend/src/services/video_indexer.py` - Azure Video Indexer integration.
- `backend/scripts/index_documents.py` - PDF ingestion and vector indexing.

## Tech Stack

- Python 3.12+
- FastAPI + Uvicorn
- LangGraph + LangChain
- Groq (OpenAI-compatible chat API)
- Azure AI Search
- Azure Video Indexer
- Azure Monitor OpenTelemetry
- yt-dlp

## Prerequisites

- Python 3.12+
- A Groq API key
- Azure subscription with:
	- Azure AI Search index
	- Azure Video Indexer account
- Credentials available to `DefaultAzureCredential` (for Video Indexer ARM calls)

## Environment Variables

Create a `.env` file in the project root and configure:

### AI and Search
- `GROQ_API_KEY`
- `GROQ_BASE_URL` (optional, defaults to `https://api.groq.com/openai/v1`)
- `GROQ_MODEL` (optional, defaults to `openai/gpt-oss-120b`)
- `EMBEDDING_MODEL` (optional, defaults to `BAAI/bge-small-en-v1.5`; downloaded locally by FastEmbed)
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_API_KEY`
- `AZURE_SEARCH_INDEX_NAME` (the deployment default is `policy-index-fastembed`)

### Video Indexer
- `AZURE_VI_ACCOUNT_ID`
- `AZURE_VI_LOCATION`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_RESOURCE_GROUP`
- `AZURE_VI_NAME` (optional, defaults to `project-brand-guardian-001`)

### Observability
- `APPLICATIONINSIGHTS_CONNECTION_STRING` (optional, enables telemetry if set)
- `LANGSMITH_TRACING` (optional, set to `true` to enable LangSmith traces)
- `LANGSMITH_API_KEY` (required when LangSmith tracing is enabled)
- `LANGSMITH_PROJECT` (optional, defaults to `policy-lens-ai`)
- `LANGSMITH_ENDPOINT` (optional, defaults to `https://api.smith.langchain.com`)
- `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, and
  `LANGCHAIN_ENDPOINT` are emitted as compatibility aliases for the installed
  LangChain version.

LangSmith tracing covers the LangGraph workflow, including the auditor's
retrieval and Groq LLM call. It is disabled by default. To enable it locally,
add the following values to `.env` or `.env.azure`:

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_your_key_here
LANGSMITH_PROJECT=policy-lens-ai
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=lsv2_your_key_here
LANGCHAIN_PROJECT=policy-lens-ai
```

The API and CLI assign each audit a trace name, `policy-lens`/`video-audit`
tags, and a session ID in trace metadata. Do not commit `.env` or `.env.azure`;
both files are intended to contain secrets.

## Local Setup

Install dependencies with your preferred toolchain.

Using `uv`:

```bash
uv sync
```

Using `pip`:

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -e .
```

## Build the Compliance Knowledge Base

Before auditing videos, index your policy PDFs (placed in `backend/data`):

```bash
uv run python backend/scripts/index_documents.py
```

This script:
- Loads PDF files from `backend/data`
- Splits text into chunks
- Embeds chunks locally with FastEmbed
- Uploads vectors to Azure AI Search

## Provision Azure Resources

The Azure automation provisions only the services required by the application:
Azure AI Search, Storage, Video Indexer, Key Vault, Log Analytics, and
Application Insights. It does not create a Container App, Container Registry,
Docker image, or Azure OpenAI resource.

Prerequisites: Azure CLI, `az login`, and permission to create resources and
role assignments in the resource group. For this Azure for Students
subscription, use an allowed region such as `centralindia`.

Preview the deployment:

```bash
az deployment group what-if \
  --resource-group pltest5 \
  --template-file infra/main.bicep \
  --parameters location=centralindia projectName=policylens
```

Create the resources and generate `.env.azure`:

```bash
az group create \
  --name pltest5 \
  --location centralindia
```

```bash
./infra/deploy.sh -g pltest5 -l centralindia -p policylens
```

The command requires only Azure CLI. It writes the Search endpoint and key,
Storage connection string, Video Indexer identifiers, subscription details,
Application Insights connection string, and disabled LangSmith tracing defaults
to `.env.azure`. It also grants the signed-in identity the
**Video Indexer Account Contributor** role required to
generate account tokens. Add your Groq key to that file:

```env
GROQ_API_KEY=gsk_your_key_here
```

#### Key Vault name already exists in deleted state

Azure Key Vault keeps deleted vault names in a soft-deleted state. If a
deployment fails with `ConflictError` and says that a vault with the same name
already exists in deleted state, the resource group can be empty while the
vault name is still reserved. List deleted vaults in the deployment region and
identify the name reported by the failed deployment:

```bash
az keyvault list-deleted \
  --output table
```

If the vault may contain data that should be retained, recover it instead of
purging it:

```bash
az keyvault recover \
  --name "<KEY_VAULT_NAME>"
```

If the vault is disposable and you have confirmed that its secrets, keys, and
certificates are no longer needed, purge it. Purging is permanent:

```bash
az keyvault purge \
  --name "<KEY_VAULT_NAME>"
```

Wait for the purge to complete, then rerun `infra/deploy.sh`. If purge
protection is enabled, Azure will not allow an early purge; recover the vault
or use a different project name instead:

```bash
./infra/deploy.sh -g pltest5 -l centralindia -p policylens2
```

#### Azure AI Search service is still deleting

If deployment fails with `ServiceDeleting` and says that the Search service
has a background operation in progress, a previous delete or update has not
finished yet. Confirm the service state and inspect the failed deployment
operations:

```bash
az search service show \
  --resource-group pltest5 \
  --name policylensdrt55jhf-search \
  --query "{name:name,provisioningState:provisioningState}" \
  --output table

az deployment group operation list \
  --resource-group pltest5 \
  --name policylens-20261017152530 \
  --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName,code:properties.statusMessage.error.code,message:properties.statusMessage.error.message}" \
  --output table
```

If `az search service show` returns `ResourceNotFound`, the old Search service
has finished deleting. Rerun the deployment with the same project name:

```bash
./infra/deploy.sh -g pltest5 -l centralindia -p policylens
```

Do not start repeated deployments immediately. Wait a few minutes and retry
with increasing delays (for example, 2, 5, and 10 minutes), checking that the
Search service is no longer in `Deleting` or `Updating` state before each
attempt. If `ResourceNotFound` is returned, proceed with the retry above. If
the operation remains stuck for an extended period, check the
Azure portal activity log or open an Azure support request with the Request ID
from the error. A different project name creates a different Search service,
but should be used only when the old service is no longer needed:

```bash
./infra/deploy.sh -g pltest5 -l centralindia -p policylens2
```

To enable LangSmith for the generated environment, export the tracing settings
before running the deployment:

```bash
LANGSMITH_TRACING=true \
LANGSMITH_API_KEY=lsv2_your_key_here \
./infra/deploy.sh -g pltest5 -l centralindia -p policylens
```

```bash
LANGSMITH_TRACING=true \
./infra/deploy.sh -g pltest5 -l centralindia -p policylens
```

If the deployment identity cannot be discovered automatically, provide its
Microsoft Entra object ID:

```bash
VIDEO_INDEXER_PRINCIPAL_ID="<OBJECT_ID>" \
  ./infra/deploy.sh -g pltest3 -l centralindia -p policylens
```

If `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and `AZURE_TENANT_ID` are already
exported, the deployment grants the role to that service principal. Otherwise,
it grants the role to the signed-in `az` user. Do not switch identities after
deployment without assigning the same role to the new identity.

The Video Indexer account uses a dedicated user-assigned managed identity and
needs **Storage Blob Data Owner** on the storage account provisioned by this
template. Azure RBAC
changes can take a few minutes to propagate. If an audit reports
`STORAGE_ACCESS_DENIED`, repair the assignment and wait for propagation before
retrying:

```bash
storage_id="$(az storage account show \
  --resource-group pltest5 \
  --name "$(az deployment group show -g pltest5 -n <DEPLOYMENT_NAME> \
    --query 'properties.outputs.storageName.value' -o tsv)" \
  --query id -o tsv)"

video_indexer_identity_id="$(az resource show \
  --resource-group pltest5 \
  --resource-type Microsoft.ManagedIdentity/userAssignedIdentities \
  --name "<VIDEO_INDEXER_IDENTITY_NAME>" \
  --query properties.clientId -o tsv)"

az role assignment create \
  --assignee "$video_indexer_identity_id" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Owner" \
  --scope "$storage_id"
```

The identity name is `<short project name>-vi-id`; use the value shown in the
deployment resources for `<VIDEO_INDEXER_IDENTITY_NAME>`. If the role already
exists, do not create a duplicate;
instead verify it with:

```bash
az role assignment list \
  --assignee "$video_indexer_identity_id" \
  --scope "$storage_id" \
  --role "Storage Blob Data Owner" \
  --include-inherited \
  --output table
```

After the role is visible, allow several minutes for Video Indexer to observe
the permission and then rerun the audit. A successful upload alone does not
prove that Video Indexer can access its backing storage; the failing operation
occurs inside the Azure service after the file upload request.

`.env.azure` is ignored by Git and contains secrets. Load it before running the
application or indexing script:

```bash
set -a
source .env.azure
set +a
uv run python backend/scripts/index_documents.py
uv run uvicorn backend.src.api.server:app --host 0.0.0.0 --port 8000
```

Run the CLI audit from the same shell after sourcing `.env.azure`:

```bash
uv run python main.py
```

FastEmbed downloads `BAAI/bge-small-en-v1.5` on first use. The same embedding
model and `policy-index-fastembed` index are used for indexing and querying.
Groq model availability can change. The default is `openai/gpt-oss-120b`; check
the active models for your key with:

```bash
curl https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $GROQ_API_KEY"
```

Delete the complete resource group and all resources with one command:

```bash
./infra/destroy.sh -g pltest3
```

The destroy command asks for confirmation. For automation:

```bash
./infra/destroy.sh -g pltest5 --yes --no-wait
```

## Run the Project

### Option 1: CLI Simulation

```bash
uv run python main.py
```

Runs a full audit flow against a hardcoded sample YouTube URL and prints a compliance report.

### Option 2: API Server

```bash
uv run uvicorn backend.src.api.server:app --reload
```

Available endpoints:
- `GET /health` - health check
- `POST /audit` - run compliance audit
- `GET /docs` - Swagger UI

Example request:

```bash
curl -X POST "http://localhost:8000/audit" \
	-H "Content-Type: application/json" \
	-d '{"video_url":"https://youtu.be/dT7S75eYhcQ"}'
```

Example response shape:

```json
{
	"session_id": "string",
	"video_id": "string",
	"status": "PASS | FAIL",
	"final_report": "string",
	"compliance_results": [
		{
			"category": "string",
			"severity": "CRITICAL | WARNING",
			"description": "string"
		}
	]
}
```

## Project Structure

```text
.
|-- main.py
|-- pyproject.toml
|-- backend/
|   |-- scripts/
|   |   |-- index_documents.py
|   |-- src/
|   |   |-- api/
|   |   |   |-- server.py
|   |   |   `-- telemetry.py
|   |   |-- graph/
|   |   |   |-- nodes.py
|   |   |   |-- state.py
|   |   |   `-- workflow.py
|   |   `-- services/
|   |       `-- video_indexer.py
|   `-- data/
`-- azure_functions/
```

## Current Status and Notes

- Core pipeline is implemented in `backend/src` and `main.py`.
- Several scaffold files are currently placeholders (empty), including:
	- `azure_functions/function_app.py`
	- `azure_functions/requirements.txt`
	- `azure_functions/host.json`
	- `azure_functions/local.settings.json`
- `backend/tests` is currently empty.

## Suggested Next Improvements

1. Add unit and integration tests for graph nodes and API contracts.
2. Add input validation and URL safety checks for video ingestion.
3. Add retry/backoff and timeout policies around external Azure calls.
4. Complete Azure Functions integration if needed.
5. Add CI for lint, tests, and deployment checks.
