targetScope = 'resourceGroup'

@maxLength(10)
@minLength(3)
param prefix string = 'clipworker'
param location string = resourceGroup().location
param vmSize string = 'Standard_D4as_v4'
@minValue(64)
param dataDiskGiB int = 128
param adminUsername string = 'clipoperator'
@secure()
param sshPublicKey string
param foundryAccountName string
@description('Existing account-scoped model-inference role definition GUID; does not change the role.')
param foundryInferenceRoleGuid string
param vmImageVersion string = 'latest'

var tags = { workload: 'clip-only', lifecycle: 'pilot' }
var suffix = uniqueString(resourceGroup().id, prefix)
var blobRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${prefix}${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['clip-runs', 'clip-approved']: {
  parent: blobs
  name: name
  properties: { publicAccess: 'None' }
}]
// A single outbound IP avoids a separate NAT gateway for this one-worker pilot.
// No inbound traffic, including SSH or VNet traffic, is accepted by the NIC.
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${prefix}-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [{
      name: 'deny-all-inbound'
      properties: {
        priority: 100
        direction: 'Inbound'
        access: 'Deny'
        protocol: '*'
        sourcePortRange: '*'
        destinationPortRange: '*'
        sourceAddressPrefix: '*'
        destinationAddressPrefix: '*'
      }
    }]
  }
}
resource ip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${prefix}-outbound'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}
resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.81.0.0/24'] }
    subnets: [{
      name: 'worker'
      properties: { addressPrefix: '10.81.0.0/24', defaultOutboundAccess: false }
    }]
  }
}
resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${prefix}-nic'
  location: location
  tags: tags
  properties: {
    networkSecurityGroup: { id: nsg.id }
    ipConfigurations: [{
      name: 'primary'
      properties: {
        privateIPAllocationMethod: 'Dynamic'
        publicIPAddress: { id: ip.id }
        subnet: { id: '${network.id}/subnets/worker' }
      }
    }]
  }
}
resource disk 'Microsoft.Compute/disks@2024-03-02' = {
  name: '${prefix}-data'
  location: location
  tags: tags
  sku: { name: 'StandardSSD_LRS' }
  properties: { creationData: { createOption: 'Empty' }, diskSizeGB: dataDiskGiB }
}
resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: '${prefix}-vm'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: '${prefix}-vm'
      adminUsername: adminUsername
      customData: base64(loadTextContent('cloud-init.yaml'))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: { publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }] }
      }
    }
    storageProfile: {
      imageReference: { publisher: 'Canonical', offer: 'ubuntu-24_04-lts', sku: 'server', version: vmImageVersion }
      osDisk: { createOption: 'FromImage', managedDisk: { storageAccountType: 'StandardSSD_LRS' }, diskSizeGB: 64 }
      dataDisks: [{ lun: 0, name: disk.name, createOption: 'Attach', managedDisk: { id: disk.id }, caching: 'None', deleteOption: 'Detach' }]
    }
    networkProfile: { networkInterfaces: [{ id: nic.id }] }
    diagnosticsProfile: { bootDiagnostics: { enabled: true } }
  }
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, vm.id, 'pull')
  scope: registry
  properties: {
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource blobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in ['clip-runs', 'clip-approved']: {
  name: guid(storage.id, vm.id, name)
  scope: containers[i]
  properties: { principalId: vm.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: blobRole }
}]
resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = { name: foundryAccountName }
resource inference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, vm.id, 'inference')
  scope: foundry
  properties: {
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryInferenceRoleGuid)
  }
}
output workerName string = vm.name
output workerId string = vm.id
output principalId string = vm.identity.principalId
output registryName string = registry.name
output registryHost string = registry.properties.loginServer
output storageAccount string = storage.name
output storageUrl string = storage.properties.primaryEndpoints.blob
output dataDiskId string = disk.id

// Validated worker setup: approved gateway role plus OpenAI User, account-scoped.
resource openAiInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, vm.id, 'openai-inference')
  scope: foundry
  properties: {
    principalId: vm.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
