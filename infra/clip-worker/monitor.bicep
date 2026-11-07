targetScope = 'resourceGroup'
param prefix string = 'clipworker'
param location string = resourceGroup().location
param workerPrincipalId string
var columns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'JobId', type: 'string' }
  { name: 'Command', type: 'string' }
  { name: 'ExitCode', type: 'int' }
  { name: 'ElapsedSeconds', type: 'real' }
  { name: 'CpuSeconds', type: 'real' }
  { name: 'MaxRssBytes', type: 'long' }
  { name: 'DiskFreeBytes', type: 'long' }
]
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}
resource table 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: workspace
  name: 'ClipWorker_CL'
  properties: { schema: { name: 'ClipWorker_CL', columns: columns }, retentionInDays: 30 }
}
resource rule 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${prefix}-metrics'
  location: location
  kind: 'Direct'
  properties: {
    streamDeclarations: { 'Custom-ClipWorker': { columns: columns } }
    destinations: { logAnalytics: [{ name: 'logs', workspaceResourceId: workspace.id }] }
    dataFlows: [{ streams: ['Custom-ClipWorker'], destinations: ['logs'], transformKql: 'source', outputStream: 'Custom-ClipWorker_CL' }]
  }
  dependsOn: [table]
}
resource publish 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rule.id,workerPrincipalId,'publish')
  scope: rule
  properties: {
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions','3913510d-42f4-4e42-8a64-420c390055eb')
  }
}
output endpoint string = rule.properties.endpoints.logsIngestion
output ruleId string = rule.properties.immutableId
output workspaceId string = workspace.properties.customerId
