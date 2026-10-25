// Parameters for main.bicep. Values come from environment variables so that
// deploy.sh can feed them from your local .env without writing secrets to disk
// or putting them on the command line.
using 'main.bicep'

param appName = readEnvironmentVariable('APP_NAME', 'call4me')
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param targetPort = int(readEnvironmentVariable('TARGET_PORT', '8000'))
param postgresAdminLogin = readEnvironmentVariable('POSTGRES_ADMIN_LOGIN', 'call4me')
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD', '')
param deployerPrincipalId = readEnvironmentVariable('DEPLOYER_PRINCIPAL_ID', '')

// Non-secret settings.
param telephony = readEnvironmentVariable('TELEPHONY', 'cartesia')
param cartesiaVoiceId = readEnvironmentVariable('CARTESIA_VOICE_ID', '')
param cartesiaAgentModel = readEnvironmentVariable('CARTESIA_AGENT_MODEL', 'claude-haiku-4.5')
param azureFoundryEndpoint = readEnvironmentVariable('AZURE_FOUNDRY_ENDPOINT', '')
param apnsKeyId = readEnvironmentVariable('APNS_KEY_ID', '')
param apnsTeamId = readEnvironmentVariable('APNS_TEAM_ID', '')
param apnsTopic = readEnvironmentVariable('APNS_TOPIC', '')
param apnsUseSandbox = bool(readEnvironmentVariable('APNS_USE_SANDBOX', 'true'))

param storageBackend = readEnvironmentVariable('STORAGE_BACKEND', 'sql')
param convexUrl = readEnvironmentVariable('CONVEX_URL', '')
