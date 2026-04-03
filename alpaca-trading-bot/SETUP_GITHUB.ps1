# ============================================================
# PUSH TRADING BOT TO GITHUB — One-Click Script
# Double-click this file to run, OR right-click > Run with PowerShell
# ============================================================

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  PUSHING TRADING BOT TO GITHUB" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Navigate to the bot folder (same folder as this script)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "Working in: $scriptDir" -ForegroundColor Gray
Write-Host ""

# Check if git is installed
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Git is not installed!" -ForegroundColor Red
    Write-Host "Download it from: https://git-scm.com/download/win" -ForegroundColor Yellow
    Write-Host ""
    Pause
    exit 1
}

Write-Host "Git found: $(git --version)" -ForegroundColor Green

# Create the .github/workflows directory structure needed for GitHub Actions
Write-Host ""
Write-Host "Creating workflow directory..." -ForegroundColor Gray
New-Item -ItemType Directory -Force -Path ".github\workflows" | Out-Null

# Initialize git repo
Write-Host "Initializing git repository..." -ForegroundColor Gray
git init
git branch -M main

# Configure git identity (required for commits)
$email = Read-Host "Enter your GitHub email address"
$name  = Read-Host "Enter your name (or press Enter for 'Daniil')"
if ([string]::IsNullOrWhiteSpace($name)) { $name = "Daniil" }

git config user.email $email
git config user.name  $name

# Add all files
Write-Host ""
Write-Host "Adding all files..." -ForegroundColor Gray
git add .
git add .github/workflows/trading_bot.yml 2>$null

# Create the first commit
git commit -m "Initial commit: Advanced Alpaca trading bot with GitHub Actions"

# Add the remote (your GitHub repo)
$remoteUrl = "https://github.com/Daniil17/alpaca-trading-bot.git"
git remote remove origin 2>$null
git remote add origin $remoteUrl

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  PUSHING TO GITHUB" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "GitHub will ask for your credentials:" -ForegroundColor Yellow
Write-Host "  Username: your GitHub username (Daniil17)" -ForegroundColor Yellow
Write-Host "  Password: a Personal Access Token (NOT your password)" -ForegroundColor Yellow
Write-Host ""
Write-Host "To create a token: GitHub > Settings > Developer settings" -ForegroundColor Yellow
Write-Host "  > Personal access tokens > Tokens (classic) > Generate new token" -ForegroundColor Yellow
Write-Host "  Select scope: 'repo' (full control)" -ForegroundColor Yellow
Write-Host ""

git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "================================================" -ForegroundColor Green
    Write-Host "  SUCCESS! Bot code is on GitHub!" -ForegroundColor Green
    Write-Host "================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next step: Add your API keys as GitHub Secrets" -ForegroundColor Cyan
    Write-Host "Go to: https://github.com/Daniil17/alpaca-trading-bot/settings/secrets/actions" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Add these 5 secrets:" -ForegroundColor Yellow
    Write-Host "  ALPACA_API_KEY      = your Alpaca API key" -ForegroundColor White
    Write-Host "  ALPACA_SECRET_KEY   = your Alpaca secret key" -ForegroundColor White
    Write-Host "  ALPACA_PAPER        = true" -ForegroundColor White
    Write-Host "  TELEGRAM_BOT_TOKEN  = your Telegram bot token" -ForegroundColor White
    Write-Host "  TELEGRAM_CHAT_ID    = your Telegram chat ID" -ForegroundColor White
    Write-Host ""
    Start-Process "https://github.com/Daniil17/alpaca-trading-bot/settings/secrets/actions"
} else {
    Write-Host ""
    Write-Host "Push failed — check your credentials and try again." -ForegroundColor Red
}

Write-Host ""
Pause
