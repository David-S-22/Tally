// ==============================================================================
// Module: Dedicated Ollama Virtual Machine
// Provisions Ubuntu 24.04 VM with cloud-init setup for Ollama, persistent disk,
// public DNS endpoint, NSG, and daily auto-shutdown.
// ==============================================================================

param location string = resourceGroup().location
param vmName string = 'vm-tally-ollama'
param vmSize string = 'Standard_B4as_v2'
param adminUsername string = 'azureuser'
param adminPublicKey string = ''
param osDiskSizeGB int = 64
param dnsLabel string = 'tally-ollama'
param autoShutdownTime string = '0000'
param autoShutdownTimeZone string = 'AUS Eastern Standard Time'

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-${vmName}'
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-SSH'
        properties: {
          priority: 1000
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
      {
        name: 'Allow-Ollama-API'
        properties: {
          priority: 1010
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '11434'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'vnet-${vmName}'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-ollama'
        properties: {
          addressPrefix: '10.0.0.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: 'pip-${vmName}'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: {
      domainNameLabel: dnsLabel
    }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: 'nic-${vmName}'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: vnet.properties.subnets[0].id
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

var cloudInitScript = '''#cloud-config
package_upgrade: true
runcmd:
  - echo "[$(date)] Starting Ollama installation..." >> /var/log/ollama-init.log
  - curl -fsSL https://ollama.com/install.sh | sh >> /var/log/ollama-init.log 2>&1
  - mkdir -p /etc/systemd/system/ollama.service.d
  - |
    cat <<'EOF' > /etc/systemd/system/ollama.service.d/override.conf
    [Service]
    Environment="OLLAMA_HOST=0.0.0.0:11434"
    Environment="OLLAMA_ORIGINS=*"
    Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"
    EOF
  - systemctl daemon-reload
  - systemctl enable --now ollama
  - echo "[$(date)] Waiting for Ollama daemon to respond..." >> /var/log/ollama-init.log
  - until curl -s http://127.0.0.1:11434/api/tags >/dev/null; do sleep 2; done
  - echo "[$(date)] Ollama online. Pulling pre-requisite models..." >> /var/log/ollama-init.log
  - OLLAMA_HOST=http://127.0.0.1:11434 ollama pull qwen2.5:0.5b >> /var/log/ollama-init.log 2>&1
  - OLLAMA_HOST=http://127.0.0.1:11434 ollama pull llama3.1:8b >> /var/log/ollama-init.log 2>&1
  - OLLAMA_HOST=http://127.0.0.1:11434 ollama pull qwen2.5:3b >> /var/log/ollama-init.log 2>&1
  - OLLAMA_HOST=http://127.0.0.1:11434 ollama pull qwen3:4b >> /var/log/ollama-init.log 2>&1
  - echo "[$(date)] Ollama automated setup complete!" >> /var/log/ollama-init.log
'''

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(cloudInitScript)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: !empty(adminPublicKey) ? {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminPublicKey
            }
          ]
        } : null
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        name: 'osdisk-${vmName}'
        caching: 'ReadWrite'
        createOption: 'FromImage'
        diskSizeGB: osDiskSizeGB
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

resource autoShutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: {
      time: autoShutdownTime
    }
    timeZoneId: autoShutdownTimeZone
    targetResourceId: vm.id
    notificationSettings: {
      status: 'Disabled'
    }
  }
}

output publicIpAddress string = publicIp.properties.ipAddress
output fqdn string = publicIp.properties.dnsSettings.fqdn
output ollamaUrl string = 'http://${publicIp.properties.dnsSettings.fqdn}:11434'
output sshCommand string = 'ssh ${adminUsername}@${publicIp.properties.dnsSettings.fqdn}'
