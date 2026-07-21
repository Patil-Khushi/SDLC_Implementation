# One-shot start script for the Code Review agent.
#
# Sets up the venv + deps, builds the ephemeral review-sandbox image (once), then clones and
# reviews the given PUBLIC repo and writes reports/<project>-<run>.md.
#
# Usage (from services/implementation/):
#   .\run_review.ps1 -RepoUrl https://github.com/owner/repo
#   .\run_review.ps1 -RepoUrl https://github.com/owner/repo -Project myapp -Skill .\SKILL.md
#
# Prereqs: Python 3.12+, Docker Desktop running, and ANTHROPIC_API_KEY set in .env.

param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Project = "review",
    [string]$Skill
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot                       # services/implementation/
$py = ".\.venv\Scripts\python.exe"

# 1. venv + dependencies
if (-not (Test-Path .venv)) {
    Write-Host "Creating virtualenv ..."
    python -m venv .venv
}
& $py -m pip install -q -r requirements.txt

# 2. .env (needs ANTHROPIC_API_KEY)
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from template - set ANTHROPIC_API_KEY in it, then re-run." -ForegroundColor Yellow
    exit 1
}

# 3. build the review-sandbox image once
$img = "sdlc-review-sandbox:latest"
if (-not (docker images -q $img)) {
    Write-Host "Building $img (first run only) ..."
    docker build -t $img ..\..\tools\review-sandbox
}

# 4. run the agent
$argv = @("scripts/run_review.py", $RepoUrl, "--project", $Project)
if ($Skill) { $argv += @("--skill", $Skill) }
& $py @argv
