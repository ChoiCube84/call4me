// Parameters for main.bicep. Values come from environment variables so that
// deploy.sh can feed them from your local .env without writing secrets to disk
// or putting them on the command line.
using 'main.bicep'

param appName = readEnvironmentVariable('APP_NAME', 'call4me')
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param targetPort = int(readEnvironmentVariable('TARGET_PORT', '8000'))
param postgresAdminLogin = readEnvironmentVariable('POSTGRES_ADMIN_LOGIN', 'call4me')
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param deployerPrincipalId = readEnvironmentVariable('DEPLOYER_PRINCIPAL_ID', '')

// Non-secret settings.
param twilioAccountSid = readEnvironmentVariable('TWILIO_ACCOUNT_SID', '')
param twilioAgentNumber = readEnvironmentVariable('TWILIO_AGENT_NUMBER', '')
param twilioFakelineNumber = readEnvironmentVariable('TWILIO_FAKELINE_NUMBER', '')
param userVerifiedCallerId = readEnvironmentVariable('USER_VERIFIED_CALLER_ID', '')
param cartesiaVoiceId = readEnvironmentVariable('CARTESIA_VOICE_ID', '')
param azureFoundryEndpoint = readEnvironmentVariable('AZURE_FOUNDRY_ENDPOINT', '')
param apnsKeyId = readEnvironmentVariable('APNS_KEY_ID', '')
param apnsTeamId = readEnvironmentVariable('APNS_TEAM_ID', '')
param apnsTopic = readEnvironmentVariable('APNS_TOPIC', '')
param apnsUseSandbox = bool(readEnvironmentVariable('APNS_USE_SANDBOX', 'true'))
