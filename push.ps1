<#
.SYNOPSIS
    把 MoviePolit 插件推送到你的 Git 仓库（Windows / PowerShell）。

.DESCRIPTION
    本目录已经是初始化并提交好的 git 仓库，脚本会自动：补提交 -> 设置 remote -> push。
    token 只作为推送时的参数临时使用，不会写入 .git/config；origin 始终是不含 token 的地址。

.EXAMPLE
    .\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git

.EXAMPLE
    $env:GIT_TOKEN = "ghp_xxxxxxxx"
    .\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git -Branch main

.EXAMPLE
    # 直连 github 不通、需要走本地代理时（常见于 127.0.0.1:7897）
    .\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git -Proxy http://127.0.0.1:7897
#>
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Branch = "main",
    [string]$Token = $env:GIT_TOKEN,
    [string]$Proxy = ""
)

# 不把原生命令的 stderr 当作终止错误（否则 git 的进度输出会中断脚本）
$ErrorActionPreference = "Continue"

Set-Location $PSScriptRoot
Write-Host "仓库目录: $(Get-Location)" -ForegroundColor Cyan

# 直连不通时自动探测系统代理（WinINET / 环境变量），避免 push 卡在 "Could not connect to server"
if (-not $Proxy) {
    foreach ($v in @($env:HTTPS_PROXY, $env:https_proxy, $env:ALL_PROXY, $env:all_proxy)) {
        if ($v) { $Proxy = $v; break }
    }
}
if (-not $Proxy) {
    $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($ie -and $ie.ProxyEnable -eq 1 -and $ie.ProxyServer) {
        $srv = ($ie.ProxyServer -split ';')[0]
        if ($srv -notmatch '=') { $srv = "http://$srv" }
        $Proxy = $srv
    }
}

# 所有要走网络的 git 命令都带上代理参数（仅本次调用生效，不写入 .git/config）
$gitNet = @()
if ($Proxy) {
    $gitNet = @("-c", "http.proxy=$Proxy", "-c", "https.proxy=$Proxy")
    Write-Host "使用代理: $Proxy" -ForegroundColor Cyan
}

if (-not (Test-Path ".git")) {
    git init -b $Branch
}
if (-not (git config user.name)) { git config user.name "MoviePolit" }
if (-not (git config user.email)) { git config user.email "moviepolit@local" }

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "没有需要提交的变更"
} else {
    git commit -m "feat: MoviePolit 流媒体上线监控插件 v1.0.0"
}

# origin 永远只写「不带 token」的地址；带 token 的地址只在推送时作为参数临时使用，
# 这样任何失败路径都不可能把 token 留在 .git/config 里。
git remote remove origin 2>$null | Out-Null
git remote add origin $RepoUrl

$pushUrl = $RepoUrl
if ($Token -and $RepoUrl.StartsWith("https://")) {
    $bare = $RepoUrl.Substring("https://".Length)
    if ($RepoUrl -match "github\.com") {
        $pushUrl = "https://x-access-token:$Token@$bare"
    } else {
        $pushUrl = "https://oauth2:$Token@$bare"
    }
}

# 先探测远端是否可达，把「连不上」和「没权限/仓库不存在」区分开
git @gitNet ls-remote $pushUrl | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "无法访问远端仓库：请确认仓库已创建、地址正确、token 有效，且网络/代理可用" -ForegroundColor Red
}

$pushed = $false
git @gitNet push $pushUrl "refs/heads/${Branch}:refs/heads/${Branch}"
if ($LASTEXITCODE -eq 0) {
    $pushed = $true
} else {
    Write-Host "推送失败，尝试与远端合并后重试..." -ForegroundColor Yellow
    git @gitNet pull --rebase origin $Branch
    git @gitNet push $pushUrl "refs/heads/${Branch}:refs/heads/${Branch}"
    if ($LASTEXITCODE -eq 0) { $pushed = $true }
}

if ($pushed) {
    Write-Host "推送完成：$RepoUrl ($Branch)" -ForegroundColor Green
    # 顺带推送版本 tag（失败不影响主流程）
    if (git tag) {
        git @gitNet push $pushUrl --tags
        if ($LASTEXITCODE -eq 0) { Write-Host "已推送 tag: $((git tag) -join ', ')" -ForegroundColor Green }
    }
    Write-Host "提示：建议在 GitHub 上把仓库设为公开，MoviePilot 插件市场才能直接拉取。" -ForegroundColor Cyan
} else {
    Write-Host "推送失败：$RepoUrl ($Branch) 未推送成功，请检查上面的 git 输出" -ForegroundColor Red
    exit 1
}
