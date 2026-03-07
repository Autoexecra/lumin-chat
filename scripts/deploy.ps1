# lumin-chat 远程部署脚本。

param(
    [string]$RemoteHost = "117.72.194.76",
    [int]$Port = 3568,
    [string]$User = "root",
    [string]$RemoteDir = "/root/lumin-chat",
    [switch]$Bootstrap
)

$ErrorActionPreference = "Stop"

# 仅打包运行所需文件，避免把本地虚拟环境和缓存带到远端。
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StageRoot = Join-Path $ProjectRoot ".dist\deploy"

if (Test-Path $StageRoot) {
    Remove-Item -Path $StageRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $StageRoot | Out-Null

New-Item -ItemType Directory -Path (Join-Path $StageRoot "src") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StageRoot "docs") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StageRoot "scripts") | Out-Null

Copy-Item -Path (Join-Path $ProjectRoot "main.py") -Destination (Join-Path $StageRoot "main.py") -Force
Copy-Item -Path (Join-Path $ProjectRoot "config.json") -Destination (Join-Path $StageRoot "config.json") -Force
Copy-Item -Path (Join-Path $ProjectRoot "requirements.txt") -Destination (Join-Path $StageRoot "requirements.txt") -Force
Copy-Item -Path (Join-Path $ProjectRoot "deploy.py") -Destination (Join-Path $StageRoot "deploy.py") -Force
Copy-Item -Path (Join-Path $ProjectRoot "docs\*.md") -Destination (Join-Path $StageRoot "docs") -Force
Copy-Item -Path (Join-Path $ProjectRoot "scripts\remote_bootstrap.sh") -Destination (Join-Path $StageRoot "scripts\remote_bootstrap.sh") -Force
Copy-Item -Path (Join-Path $ProjectRoot "scripts\smoke_test.py") -Destination (Join-Path $StageRoot "scripts\smoke_test.py") -Force
Copy-Item -Path (Join-Path $ProjectRoot "scripts\docker_ubuntu_test.py") -Destination (Join-Path $StageRoot "scripts\docker_ubuntu_test.py") -Force
Copy-Item -Path (Join-Path $ProjectRoot "src\*.py") -Destination (Join-Path $StageRoot "src") -Force

Get-ChildItem -Path $StageRoot -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $StageRoot -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force -ErrorAction SilentlyContinue

$Remote = "$User@$RemoteHost`:$RemoteDir/"

ssh -p $Port "$User@$RemoteHost" "mkdir -p $RemoteDir && rm -f $RemoteDir/main.py $RemoteDir/config.json $RemoteDir/requirements.txt $RemoteDir/deploy.py && rm -rf $RemoteDir/src $RemoteDir/docs $RemoteDir/scripts"
& scp -P $Port -r (Join-Path $StageRoot "main.py") (Join-Path $StageRoot "config.json") (Join-Path $StageRoot "requirements.txt") (Join-Path $StageRoot "deploy.py") (Join-Path $StageRoot "src") (Join-Path $StageRoot "docs") (Join-Path $StageRoot "scripts") $Remote

if ($Bootstrap) {
    ssh -p $Port "$User@$RemoteHost" "bash $RemoteDir/scripts/remote_bootstrap.sh $RemoteDir > /tmp/lumin_chat_bootstrap.log 2>&1"
}

Write-Host "部署完成: $RemoteDir"
