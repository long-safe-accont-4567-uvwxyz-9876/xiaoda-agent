Remove-Item Env:SAFE_RM_ALLOWED_PATH -ErrorAction SilentlyContinue
Remove-Item Env:SAFE_RM_DENIED_PATH -ErrorAction SilentlyContinue
Remove-Item Env:SAFE_RM_DISABLED -ErrorAction SilentlyContinue
Remove-Item Env:SAFE_RM_AUTO_ADD_TEMP -ErrorAction SilentlyContinue
Remove-Item Env:SAFE_RM_PROTECTION_FLAG -ErrorAction SilentlyContinue
Remove-Item Env:SAFE_RM_TEMP -ErrorAction SilentlyContinue
[Environment]::SetEnvironmentVariable('SAFE_RM_ALLOWED_PATH', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_DENIED_PATH', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_DISABLED', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_AUTO_ADD_TEMP', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_PROTECTION_FLAG', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_TEMP', $null, 'User')
[Environment]::SetEnvironmentVariable('SAFE_RM_ALLOWED_PATH', $null, 'Machine')
[Environment]::SetEnvironmentVariable('SAFE_RM_DENIED_PATH', $null, 'Machine')
[Environment]::SetEnvironmentVariable('SAFE_RM_DISABLED', $null, 'Machine')
[Environment]::SetEnvironmentVariable('SAFE_RM_AUTO_ADD_TEMP', $null, 'Machine')
[Environment]::SetEnvironmentVariable('SAFE_RM_PROTECTION_FLAG', $null, 'Machine')
[Environment]::SetEnvironmentVariable('SAFE_RM_TEMP', $null, 'Machine')
Write-Output "SAFE_RM vars cleared"
python f:\naxida\xiaoda-agent\_api_commit.py