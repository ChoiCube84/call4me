// call4me on Azure: Container Apps + PostgreSQL Flexible Server + Key Vault + App Insights.
// Deploy with infra/deploy.sh (see infra/README.md).
targetScope = 'resourceGroup'

@description('Short name used as a prefix for resources.')
param appName string = 'call4me'

param location string = resourceGroup().location

@description('Container image. The first deploy uses a public placeholder; deploy.sh then builds into ACR and redeploys.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Target port of the container. 8000 for call4me; 80 for the quickstart placeholder.')
param targetPort int = 8000

param postgresAdminLogin string = 'call4me'

@secure()
@description('PostgreSQL admin password. Stored in Key Vault as part of `database-url`.')
param postgresAdminPassword string = ''

@description('Object ID of the person running the deploy, granted Key Vault Secrets Officer so deploy.sh can set secrets. Empty to skip.')
param deployerPrincipalId string = ''

@allowed(['sql', 'convex'])
param storageBackend string = 'sql'
param convexUrl string = ''

// ---- Non-secret app settings (passed as plain env vars) ----
@description('Who places calls: cartesia (a Cartesia agent dials real numbers) or phone (fake line; the /phone page plays the rep).')
@allowed(['cartesia', 'phone'])
param telephony string = 'cartesia'
param cartesiaVoiceId string = ''
@description('Model the Cartesia agent runs (GET https://api.cartesia.ai/v1/agents/models).')
param cartesiaAgentModel string = 'claude-haiku-5.5'
param azureFoundryEndpoint string = ''
param apnsKeyId string = ''
param apnsTeamId string = ''
param apnsTopic string = ''
param apnsUseSandbox bool = true

var suffix = uniqueString(resourceGroup().id)
var acrName = take('${replace(appName, '-', '')}${suffix}', 50)
var kvName = take('kv-${appName}-${suffix}', 24)
var pgName = '${appName}-pg-${suffix}'
var acrLoginServer = '${acrName}${environment().suffixes.acrLoginServer}'

// Built-in role definition IDs.
var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'
var kvSecretsOfficerRole = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

// ---------------------------------------------------------------- monitoring
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${appName}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------------------------------------------------------------- identity + registry
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-id'
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPullRole)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- key vault
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, kvSecretsUserRole)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRole)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(vault.id, deployerPrincipalId, kvSecretsOfficerRole)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficerRole)
    principalId: deployerPrincipalId
  }
}

// Secrets whose values this template knows. The rest (api-token, cartesia-api-key,
// typesafe-api-key, anthropic-api-key, azure-foundry-key, apns-auth-key) are set by
// deploy.sh; they are not created here with placeholder values because config.py
// would load a placeholder as a real value, and a redeploy would overwrite real
// secrets. Names use dashes: config.py maps `cartesia_api_key` -> `cartesia-api-key`.
resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (storageBackend == 'sql') {
  parent: vault
  name: 'database-url'
  properties: {
    value: 'postgresql+asyncpg://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres!.properties.fullyQualifiedDomainName}:5432/call4me?ssl=require'
  }
}

resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'applicationinsights-connection-string'
  properties: {
    value: appInsights.properties.ConnectionString
  }
}

// ---------------------------------------------------------------- postgres
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (storageBackend == 'sql') {
  name: pgName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (storageBackend == 'sql') {
  parent: postgres
  name: 'call4me'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// 0.0.0.0 - 0.0.0.0 is Azure's "allow access from Azure services" rule.
resource postgresAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (storageBackend == 'sql') {
  parent: postgres
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ---------------------------------------------------------------- container apps
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// The app's public FQDN is known before the app exists, so PUBLIC_BASE_URL can be set
// on the first deploy: <app>.<environment default domain>.
var appFqdn = '${appName}.${env.properties.defaultDomain}'
var usesAcr = startsWith(containerImage, acrLoginServer)

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull, kvSecretsUser, postgresDb, databaseUrlSecret, appInsightsSecret]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      // HTTP/1.1 ingress carries the WebSockets (/calls/{id}/live, /phone/ws) as is.
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: usesAcr ? [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'AZURE_KEY_VAULT_URL', value: vault.properties.vaultUri }
            // DefaultAzureCredential needs the client ID to pick the user-assigned identity.
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'PUBLIC_BASE_URL', value: 'https://${appFqdn}' }
            { name: 'USE_MOCKS', value: 'false' }
            { name: 'STORAGE_BACKEND', value: storageBackend }
            { name: 'CONVEX_URL', value: convexUrl }
            { name: 'TELEPHONY', value: telephony }
            { name: 'CARTESIA_VOICE_ID', value: cartesiaVoiceId }
            { name: 'CARTESIA_AGENT_MODEL', value: cartesiaAgentModel }
            { name: 'AZURE_FOUNDRY_ENDPOINT', value: azureFoundryEndpoint }
            { name: 'APNS_KEY_ID', value: apnsKeyId }
            { name: 'APNS_TEAM_ID', value: apnsTeamId }
            { name: 'APNS_TOPIC', value: apnsTopic }
            { name: 'APNS_USE_SANDBOX', value: apnsUseSandbox ? 'true' : 'false' }
          ]
          probes: targetPort == 8000 ? [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ] : []
        }
      ]
      // Always one warm replica: Cartesia's webhooks and the app's live socket must
      // reach the process that runs the call, and in-memory call state (LiveHub,
      // AnswerBroker) assumes a single replica.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appFqdn string = app.properties.configuration.ingress.fqdn
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output keyVaultName string = vault.name
output keyVaultUrl string = vault.properties.vaultUri
output postgresHost string = storageBackend == 'sql' ? postgres!.properties.fullyQualifiedDomainName : ''
output identityClientId string = identity.properties.clientId
