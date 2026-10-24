#!/usr/bin/env bash
# Deploy call4me to Azure Container Apps. See infra/README.md.
#
#   RESOURCE_GROUP=call4me-rg LOCATION=westus2 ./infra/deploy.sh
#
# Reads settings and secrets from .env (ENV_FILE to override). Secret values
# are never echoed or passed on the command line: they reach Key Vault via
# `az keyvault secret set --file`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RESOURCE_GROUP="${RESOURCE_GROUP:-call4me-rg}"
LOCATION="${LOCATION:-westus2}"
ENV_FILE="${ENV_FILE:-.env}"
export APP_NAME="${APP_NAME:-call4me}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
PLACEHOLDER_IMAGE="mcr.microsoft.com/k8se/quickstart:latest"

log() { printf '==> %s\n' "$*" >&2; }

# --------------------------------------------------------------------------- #
# 1. Prerequisites
# --------------------------------------------------------------------------- #
command -v az >/dev/null || { echo "Azure CLI (az) not found." >&2; exit 1; }
if ! az account show --only-show-errors >/dev/null 2>&1; then
  echo "Not logged in. Run: az login" >&2
  exit 1
fi
log "Subscription: $(az account show --query name -o tsv)"

# Load .env into the environment without printing anything. Handles
# KEY=value, KEY="value", and trailing "  # comments".
load_env() {
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    val="${line#*=}"
    if [[ "$val" =~ ^[[:space:]]*\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
      val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^[[:space:]]*\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
      val="${BASH_REMATCH[1]}"
    else
      val="$(printf '%s' "$val" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    fi
    export "$key=$val"
  done < "$1"
}

if [[ -f "$ENV_FILE" ]]; then
  log "Loading settings from $ENV_FILE"
  load_env "$ENV_FILE"
else
  log "No $ENV_FILE found; deploying with empty settings"
fi
# Never let a local dev flag leak into prod.
export USE_MOCKS=false

# --------------------------------------------------------------------------- #
# 2. Resource group + who is deploying
# --------------------------------------------------------------------------- #
log "Resource group $RESOURCE_GROUP ($LOCATION)"
az group create -n "$RESOURCE_GROUP" -l "$LOCATION" --only-show-errors -o none

export DEPLOYER_PRINCIPAL_ID="${DEPLOYER_PRINCIPAL_ID:-$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)}"

# Reuse the Postgres admin password from a previous deploy so redeploys don't
# rotate it under the running app; otherwise generate one.
EXISTING_KV="$(az keyvault list -g "$RESOURCE_GROUP" --query '[0].name' -o tsv 2>/dev/null || true)"
if [[ -z "${POSTGRES_ADMIN_PASSWORD:-}" && -n "$EXISTING_KV" ]]; then
  POSTGRES_ADMIN_PASSWORD="$(az keyvault secret show --vault-name "$EXISTING_KV" \
    --name postgres-admin-password --query value -o tsv 2>/dev/null || true)"
fi
if [[ -z "${POSTGRES_ADMIN_PASSWORD:-}" ]]; then
  POSTGRES_ADMIN_PASSWORD="Pg1-$(openssl rand -hex 16)"
fi
export POSTGRES_ADMIN_PASSWORD

deploy() {
  # $1 = image, $2 = target port
  CONTAINER_IMAGE="$1" TARGET_PORT="$2" az deployment group create \
    -g "$RESOURCE_GROUP" -n "call4me-$(date +%s)" \
    --template-file infra/main.bicep --parameters infra/main.bicepparam \
    --only-show-errors --query properties.outputs -o json
}

output() { printf '%s' "$OUTPUTS" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }

# --------------------------------------------------------------------------- #
# 3. Infrastructure (keep the current image if the app already exists)
# --------------------------------------------------------------------------- #
CURRENT_IMAGE="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query 'properties.template.containers[0].image' -o tsv 2>/dev/null || true)"
if [[ -n "$CURRENT_IMAGE" && "$CURRENT_IMAGE" != "$PLACEHOLDER_IMAGE" ]]; then
  FIRST_IMAGE="$CURRENT_IMAGE"; FIRST_PORT=8000
else
  FIRST_IMAGE="$PLACEHOLDER_IMAGE"; FIRST_PORT=80
fi
log "Deploying infrastructure (this takes ~10 minutes the first time)"
OUTPUTS="$(deploy "$FIRST_IMAGE" "$FIRST_PORT")"
ACR_NAME="$(output acrName)"
ACR_LOGIN_SERVER="$(output acrLoginServer)"
KV_NAME="$(output keyVaultName)"

# --------------------------------------------------------------------------- #
# 4. Secrets -> Key Vault (names use dashes; see backend/config.py)
# --------------------------------------------------------------------------- #
set_secret() {
  # $1 = secret name, $2 = file to read the value from. Retries while the
  # Key Vault role assignment propagates.
  local i
  for i in 1 2 3 4 5 6; do
    if az keyvault secret set --vault-name "$KV_NAME" --name "$1" --file "$2" \
      --encoding utf-8 --only-show-errors -o none 2>/dev/null; then
      log "  set $1"
      return 0
    fi
    sleep 10
  done
  echo "Could not set secret $1 (do you have Key Vault Secrets Officer on $KV_NAME?)" >&2
  return 1
}

set_secret_from_env() {
  # $1 = env var name, $2 = secret name. Skips unset/empty values.
  local value="${!1:-}"
  [[ -n "$value" ]] || { log "  skip $2 ($1 empty)"; return 0; }
  local tmp
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  printf '%s' "$value" > "$tmp"
  set_secret "$2" "$tmp" || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
}

log "Writing secrets to Key Vault $KV_NAME"
set_secret_from_env TWILIO_AUTH_TOKEN twilio-auth-token
set_secret_from_env CARTESIA_API_KEY cartesia-api-key
set_secret_from_env TYPESAFE_API_KEY typesafe-api-key
set_secret_from_env ANTHROPIC_API_KEY anthropic-api-key
set_secret_from_env AZURE_FOUNDRY_KEY azure-foundry-key
set_secret_from_env POSTGRES_ADMIN_PASSWORD postgres-admin-password
if [[ -n "${APNS_AUTH_KEY_PATH:-}" && -f "${APNS_AUTH_KEY_PATH}" ]]; then
  set_secret apns-auth-key "$APNS_AUTH_KEY_PATH"
else
  log "  skip apns-auth-key (APNS_AUTH_KEY_PATH not a file)"
fi
# database-url and applicationinsights-connection-string are written by Bicep.

# --------------------------------------------------------------------------- #
# 5. Build the image in ACR and roll the app onto it
# --------------------------------------------------------------------------- #
IMAGE="$ACR_LOGIN_SERVER/call4me:$IMAGE_TAG"
log "Building $IMAGE in ACR"
az acr build -r "$ACR_NAME" -t "call4me:$IMAGE_TAG" -t "call4me:latest" \
  --only-show-errors -o none .

log "Deploying app image"
OUTPUTS="$(deploy "$IMAGE" 8000)"
APP_URL="$(output appUrl)"
APP_FQDN="$(output appFqdn)"

cat <<EOF

call4me is deployed.

  App URL:        $APP_URL
  Health check:   $APP_URL/health

Configure Twilio (Console > Phone Numbers > ${TWILIO_AGENT_NUMBER:-your agent number}):
  Voice webhook:      $APP_URL/twilio/voice
  Status callback:    $APP_URL/twilio/status
  Media Stream URL:   wss://$APP_FQDN/media

Point the iOS app's backend URL at: $APP_URL
EOF
