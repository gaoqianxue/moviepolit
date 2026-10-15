<#
.SYNOPSIS
    把 MoviePolit 插件推送到你的 Git 仓库（Windows / PowerShell）。

.DESCRIPTION
    本目录已经是初始化并提交好的 git 仓库，脚本会自动：补提交 -> 设置 remote -> push。
    推送结束后会把 remote 地址还原成不带 token 的版本，避免 token 明文留在 .git/config。

.EXAMPLE
    .\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git

.EXAMPLE
    $env:GIT_TOKEN = "ghp_xxxxxxxx"
    .\push.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git -Branch main
#>
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Branch = "main",
    [string]$Token = $env:GIT_TOKEN
)

# 不把原生命令的 stderr 当作终止错误（否则 git 的进度输出会中断脚本）
$ErrorActionPreference = "Continue"

Set-Location $PSScriptRoot
Write-Host "仓库目录: $(Get-Location)" -ForegroundColor Cyan

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

$pushUrl = $RepoUrl
if ($Token -and $RepoUrl.StartsWith("https://")) {
    $bare = $RepoUrl.Substring("https://".Length)
    if ($RepoUrl -match "github\.com") {
        $pushUrl = "https://x-access-token:$Token@$bare"
    } else {
        $pushUrl = "https://oauth2:$Token@$bare"
    }
}

git remote remove origin 2>$null | Out-Null
git remote add origin $pushUrl

git push -u origin $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "推送失败，尝试与远端合并后重试..." -ForegroundColor Yellow
    git pull --rebase origin $Branch
    git push -u origin $Branch
}

# 还原成不含 token 的地址
git remote set-url origin $RepoUrl
if ($LASTEXITCODE -eq 0) {
    Write-Host "完成：$RepoUrl ($Branch)" -ForegroundColor Green
} else {
    Write-Host "推送可能失败，请查看上面的 git 输出" -ForegroundColor Red
}
