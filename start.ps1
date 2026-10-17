$ErrorActionPreference = "Stop"

docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Trip Planner is running:" -ForegroundColor Green
Write-Host "  Frontend: http://localhost"
Write-Host "  Backend:  http://localhost:8000"
Write-Host "  API docs: http://localhost:8000/docs"
