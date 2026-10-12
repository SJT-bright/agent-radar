param([string]$Version="1.9.0",[switch]$SkipInstaller)
$ErrorActionPreference="Stop"
$root=Split-Path $PSScriptRoot -Parent
$dest=Join-Path $root 'build/windows'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
python -m PyInstaller --noconfirm --clean --name radar-worker --distpath "$dest/worker" --workpath "$dest/pyinstaller" --specpath "$dest" --paths "$root" --paths "$root/collector" --add-data "$root/collector;collector" --add-data "$root/supervisor;supervisor" "$root/scripts/windows_worker.py"
if($LASTEXITCODE -ne 0){throw 'Worker packaging failed'}
dotnet publish "$PSScriptRoot/AgentRadar.Windows/AgentRadar.Windows.csproj" -c Release -r win-x64 --self-contained true -p:Version=$Version -p:DebugType=None -o "$dest/publish"
if($LASTEXITCODE -ne 0){throw 'Windows publish failed'}
if(Test-Path "$root/LICENSE"){Copy-Item "$root/LICENSE" "$dest/publish/LICENSE.txt"}
New-Item -ItemType Directory -Force -Path "$dest/publish/Resources" | Out-Null
Copy-Item "$dest/worker/radar-worker" "$dest/publish/Resources/radar-worker" -Recurse -Force
New-Item -ItemType Directory -Force -Path "$dest/release" | Out-Null
Compress-Archive -Path "$dest/publish/*" -DestinationPath "$dest/release/AgentRadar-$Version-Windows-x64-Portable.zip" -Force
if(-not $SkipInstaller){
 $iscc=(Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
 if(-not $iscc){$iscc="${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"}
 $language=Join-Path $PSScriptRoot "installer/ChineseSimplified.isl"
 if(-not (Test-Path $language)){Invoke-WebRequest -Uri "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl" -OutFile $language}
 & $iscc "/DAppVersion=$Version" "/DPublishDir=$dest/publish" "$PSScriptRoot/installer/AgentRadar.iss"
 if($LASTEXITCODE -ne 0){throw 'Installer failed'}
}
Get-ChildItem "$dest/release" -File | ForEach-Object{(Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()+"  "+$_.Name} | Set-Content "$dest/release/SHA256SUMS-Windows" -Encoding ascii
