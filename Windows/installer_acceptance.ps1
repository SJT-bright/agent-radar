param([ValidateSet('install','uninstall')][string]$Mode='install')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$dest=Join-Path $root 'build/windows'
$install=Join-Path $dest '安装验收 任务雷达'
$proof=Join-Path $dest 'installer-acceptance.json'
$marker=Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'AgentRadar/installer-preservation-fixture.json'
if($Mode -eq 'install'){
 $setup=Get-ChildItem "$dest/release/*-Setup.exe" | Select-Object -First 1
 if(-not $setup){throw 'Installer missing'}
 if(Test-Path $marker){throw 'Fixture marker already exists; refusing overwrite'}
 New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
 '{"fixture":"isolated CI user data; no external session","preserve":true}' | Set-Content -Encoding utf8 $marker
 $before=(Get-FileHash $marker -Algorithm SHA256).Hash
 $p=Start-Process -FilePath $setup.FullName -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',"/DIR=`"$install`"",'/TASKS=') -Wait -PassThru
 if($p.ExitCode -ne 0){throw "Silent installer exit $($p.ExitCode)"}
 if(-not (Test-Path "$install/AgentRadar.exe") -or -not (Test-Path "$install/Resources/radar-worker/radar-worker.exe")){throw 'Installed product or worker missing'}
 $original=(Get-FileHash "$dest/publish/AgentRadar.exe" -Algorithm SHA256).Hash
 $installed=(Get-FileHash "$install/AgentRadar.exe" -Algorithm SHA256).Hash
 if($original -ne $installed){throw 'Installed native program differs from packaged program'}
 @{installed=$true;installed_exe_matches=$true;chinese_space_path=$true;data_preserved=$true;marker_hash=$before;all_passed=$false} | ConvertTo-Json | Set-Content -Encoding utf8 $proof
} else {
 $j=Get-Content $proof -Raw | ConvertFrom-Json
 $uninstall=Join-Path $install 'unins000.exe'
 if(-not (Test-Path $uninstall)){throw 'Uninstaller missing'}
 $p=Start-Process -FilePath $uninstall -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -Wait -PassThru
 if($p.ExitCode -ne 0){throw 'Uninstall failed'}
 if((Get-FileHash $marker -Algorithm SHA256).Hash -ne $j.marker_hash){throw 'User data marker changed during install or uninstall'}
 if(Test-Path "$install/AgentRadar.exe"){throw 'Installed executable still exists after uninstall'}
 $j.all_passed=$true;$j | Add-Member -NotePropertyName uninstalled -NotePropertyValue $true
 $j | ConvertTo-Json | Set-Content -Encoding utf8 $proof
 Remove-Item $marker
}
