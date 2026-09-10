# Thin wrapper - the real logic lives in E:\tools\deploy-to-ha.ps1
# Usage:  .\deploy.ps1          deploy
#         .\deploy.ps1 -DryRun  show what would change
param([switch]$DryRun)
& 'E:\tools\deploy-to-ha.ps1' -Domain 'huawei_ont' -Source "$PSScriptRoot\custom_components\huawei_ont" -DryRun:$DryRun
exit $LASTEXITCODE
