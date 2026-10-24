# Deploying call4me to Azure

`deploy.sh` stands up the backend on **Azure Container Apps**, with **PostgreSQL
Flexible Server**, **Key Vault**, and **Application Insights** (CLAUDE.md section 15).

```bash
az login
./infra/deploy.sh                                   # call4me-rg in northcentralus
RESOURCE_GROUP=x LOCATION=swedencentral ./infra/deploy.sh
```

Prerequisites: Azure CLI (includes Bicep), `openssl`, `python3`, `curl`, and a filled-in
`.env` (template: `.env.example`). Docker is used only if `az acr build` is blocked (it is on
Azure for Students). The image build also builds the web app (`web/`) in a Node stage.

## What gets created (`main.bicep`)

| Resource | Notes |
|---|---|
| Log Analytics workspace + Application Insights | Logs, traces, metrics (30-day retention). Log records are redacted before export (`backend/telemetry.py`). |
| Container Registry (Basic) | Holds the app image. |
| User-assigned managed identity | `AcrPull` on the registry, `Key Vault Secrets User` on the vault. No passwords between Azure services except Postgres. |
| Key Vault (RBAC mode, soft delete) | All secrets. The app reads them at startup with its managed identity (`backend/config.py`). |
| PostgreSQL Flexible Server (Burstable B1ms, v16) | Database `call4me`, TLS required, firewall "allow Azure services". |
| Container Apps environment + app | External HTTPS ingress on port 8000, `/health` liveness and readiness probes, exactly 1 replica. |

**Why exactly one replica:** Cartesia's webhooks, the app's live WebSocket, and the
call's in-memory state (`LiveHub`, `AnswerBroker`, the Cartesia call registry) must all
reach the same process. `minReplicas: 1` also means no cold start when a call starts.
Scaling out would need that state in a shared store (e.g. Azure Cache for Redis)
first.

The container gets these env vars: `AZURE_KEY_VAULT_URL`, `AZURE_CLIENT_ID` (so
`DefaultAzureCredential` picks the managed identity), `PUBLIC_BASE_URL` (its own
`https://` address, which Cartesia calls back), `USE_MOCKS=false`, `TELEPHONY` (default
`cartesia`), `CARTESIA_VOICE_ID`, `CARTESIA_AGENT_MODEL`, and the optional
`AZURE_FOUNDRY_ENDPOINT` / `APNS_*` settings.

## Secrets (Key Vault)

`config.py` reads each secret setting from the Key Vault secret with the same name in
dashes (`cartesia_api_key` -> `cartesia-api-key`). Values never go on a command line or
into logs: `deploy.sh` writes them with `az keyvault secret set --file`.

| Secret | Set by |
|---|---|
| `database-url`, `applicationinsights-connection-string` | Bicep |
| `cartesia-api-key`, `typesafe-api-key`, `anthropic-api-key`, `azure-foundry-key` | `deploy.sh`, from `.env` |
| `api-token` | `deploy.sh`: generated on the first deploy, then reused |
| `postgres-admin-password` | `deploy.sh`: generated on the first deploy, then reused |
| `apns-auth-key` | `deploy.sh`, from the file at `APNS_AUTH_KEY_PATH` (paid Apple account only) |

## What the script does

1. Checks `az login`. Reads the subscription's allowed regions (Azure for Students allows
   only `northcentralus`, `swedencentral`, `belgiumcentral`, `denmarkeast`,
   `norwayeast`) and stops early if `LOCATION` isn't one of them.
2. Registers the resource providers it needs and creates the resource group.
3. Deploys the infrastructure. The first time, it uses a public placeholder image,
   because the registry doesn't exist yet.
4. Writes the secrets to Key Vault.
5. Builds the image with `az acr build`. Free-credit subscriptions sometimes block ACR
   Tasks; then it builds locally with `docker buildx --platform linux/amd64` and pushes.
   Then it rolls the app onto the new image.
6. Waits for `/health` and prints the web app URL (`/`), the judges view (`/dashboard/`),
   and how to read the access code and the logs.

Re-running is safe. It keeps the running image during the infrastructure step, reuses
the stored Postgres password and access token, and rolls out a new revision.

## After deploy

- **Cartesia:** on start, the app creates or updates its Cartesia agent, tools, and
  webhook to point at the Azure URL, and uses (or provisions) a Cartesia phone number.
  Check the logs for `Cartesia ready: agent ..., calling from +1...`.
- **Logs:** Log Analytics (`ContainerAppConsoleLogs_CL`; the query is printed by `deploy.sh`), or
  Application Insights > Logs. `az containerapp logs show` crashes on some CLI versions
  (`KeyError: 'eventStreamEndpoint'`).
- **Switch to the backup demo:** `TELEPHONY=phone ./infra/deploy.sh`. The rep page is then
  `https://<app>/phone`, and it works from any phone because ingress is HTTPS.

## Cost and cleanup

Roughly $1–3 a day from the student credits: Postgres B1ms, one always-on Container App
replica, and the Basic registry. Delete everything with
`az group delete -n call4me-rg`. The Key Vault stays soft-deleted for 7 days under the
same name. To redeploy into a fresh group sooner, run
`az keyvault purge -n <vault>` first.

## Production path (beyond the hackathon)

- Postgres on a private VNet (Container Apps VNet integration) instead of "allow Azure
  services", with Entra ID auth instead of a password.
- Per-user sign-in (Entra External ID / Sign in with Apple) instead of one shared
  access token.
- Shared call state in Redis so the app can scale beyond one replica, plus a second
  region.
- Azure Communication Services for telephony, with per-call cost tracking.
