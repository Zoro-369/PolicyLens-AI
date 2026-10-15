targetScope = 'resourceGroup'

@description('Lowercase prefix used for globally unique Azure resource names.')
@minLength(3)
param projectName string = 'policylens'
param location string = resourceGroup().location
param searchIndexName string = 'policy-index-fastembed'
@description('Object ID of the identity that calls the Video Indexer ARM API locally.')
param videoIndexerPrincipalId string = ''

var suffix = uniqueString(resourceGroup().id)
var shortName = '${take(projectName, 12)}${take(suffix, 8)}'
var searchName = '${shortName}-search'
var storageName = 'pl${take(suffix, 20)}st'
var logName = '${shortName}-logs'
var insightsName = '${shortName}-appinsights'
var videoIndexerName = '${shortName}-vi'
var keyVaultName = '${take(replace(shortName, '-', ''), 18)}kv'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    publicNetworkAccess: 'enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

resource videoIndexer 'Microsoft.VideoIndexer/accounts@2024-01-01' = {
  name: videoIndexerName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    storageServices: {
      resourceId: storage.id
    }
  }
}

resource videoIndexerStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, videoIndexer.name, 'StorageBlobDataContributor')
  scope: storage
  properties: {
    principalId: videoIndexer.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

resource videoIndexerCallerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(videoIndexerPrincipalId)) {
  name: guid(videoIndexer.id, videoIndexerPrincipalId, 'VideoIndexerAccountContributor')
  scope: videoIndexer
  properties: {
    principalId: videoIndexerPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3f99eaab-6f59-4877-adf5-1cacd22e20b0')
  }
}

output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output storageName string = storage.name
output videoIndexerName string = videoIndexer.name
output videoIndexerAccountId string = videoIndexer.properties.accountId
output keyVaultName string = keyVault.name
output appInsightsName string = appInsights.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output searchIndexName string = searchIndexName
