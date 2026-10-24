# Deploying call4me to Azure

One script stands up the backend on **Azure Container Apps**, with
**PostgreSQL Flexible Server**, **Key Vault**, and **Application Insights**
(CLAUDE.md section 15).

## What gets created (`main.bicep`)

| Resource | Notes |
|---|---|
| Log Analytics workspace + Application Insights | Logs, traces, and metrics. The connection string is written to Key Vault. |
| Azure Container Registry (Basic) | Images are built in the cloud with `az acr build` (no local Docker needed). |
| User-assigned managed identity | `AcrPull` on the registry and `Key Vault Secrets User` on the vault. |
| Key Vault (RBAC mode) | All secrets. The app loads them at startup (`backend/config.py`). |
| PostgreSQL Flexible Server (B1ms, v16) | Database `call4me`. Firewall rule "allow Azure services". |
| Container Apps environment + app | External ingress on port 8000, `minReplicas: 1` (no cold starts). |

WebSockets (`/media` for Twilio, `/calls/{id}/live` for the app) work over the
default HTTP/1.1 ingress with `transport: auto`; nothing extra to turn on.
`maxReplicas` is also 1 because live-call state (`LiveHub`, `AnswerBroker`) is
in process memory.

The container gets these env vars: `AZURE_KEY_VAULT_URL`, `AZURE_CLIENT_ID`
(so `DefaultAzureCredential` uses the user-assigned identity), `PUBLIC_BASE_URL`
(the app's own `https://` FQDN), `USE_MOCKS=false`, and the non-secret settings
(`TWILIO_ACCOUNT_SID`, numbers, `CARTESIA_VOICE_ID`, `APNS_KEY_ID`,
`APNS_TEAM_ID`, `APNS_TOPIC`, `APNS_USE_SANDBOX`, `AZURE_FOUNDRY_ENDPOINT`).

## Secrets in Key Vault

`config.py` reads each secret field from a Key Vault secret with the same name,
using dashes (`twilio_auth_token` -> `twilio-auth-token`).

| Secret | Set by |
|---|---|
| `database-url` | Bicep (built from the server FQDN and admin password, `?ssl=require`) |
| `applicationinsights-connection-string` | Bicep |
| `twilio-auth-token`, `cartesia-api-key`, `typesafe-api-key`, `anthropic-api-key`, `azure-foundry-key` | `deploy.sh`, from your `.env` |
| `apns-auth-key` | `deploy.sh`, contents of the file at `APNS_AUTH_KEY_PATH` |
| `postgres-admin-password` | `deploy.sh` (reused on redeploys so the password doesn't rotate) |

Bicep doesn't create placeholder values for the `.env` secrets: the app would
load a placeholder as if it were real, and every redeploy would overwrite the
real value. Missing secrets just fall back to env (empty).

## Deploy

Prerequisites: Azure CLI (`az`, includes Bicep), `openssl`, `python3`, and
`az login` to the subscription with the hackathon credits.

```bash
cp .env.example .env    # fill in real values (never commit it)
RESOURCE_GROUP=call4me-rg LOCATION=westus2 ./infra/deploy.sh
```

The script:

1. Checks `az login`, creates the resource group.
2. Deploys the infrastructure (a public placeholder image the first time,
   because the registry doesn't exist yet).
3. Writes secrets from `.env` to Key Vault with `--file` (values never echoed
   or put on the command line).
4. Builds the image in ACR (`az acr build`) and redeploys the app onto it.
5. Prints the app URL and the Twilio webhooks to configure:
   - Voice webhook: `https://<app>/twilio/voice`
   - Status callback: `https://<app>/twilio/status`
   - Media Stream: `wss://<app>/media`

Optional env vars: `APP_NAME` (default `call4me`), `IMAGE_TAG` (default: git
short SHA), `ENV_FILE` (default `.env`), `POSTGRES_ADMIN_PASSWORD`.

Re-running the script is safe: it keeps the current image during the
infrastructure step and reuses the stored Postgres password.

## After deploy

- Point the iOS app at the printed URL.
- Use `APNS_USE_SANDBOX=true` for development builds from Xcode and `false`
  for TestFlight / App Store builds.
- Logs: `az containerapp logs show -g call4me-rg -n call4me --follow`, or
  Application Insights > Logs. Log records pass through the redaction filter
  in `backend/telemetry.py` before export.

## Not validated here

This template was written without `az`/`bicep` available, so run
`az bicep build --file infra/main.bicep` (or the first deploy) to catch any
schema issues.
