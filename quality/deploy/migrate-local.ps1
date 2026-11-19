# 本地开发（Windows PowerShell）执行归档库迁移，等价于 quality/deploy/migrate.sh。
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File quality\deploy\migrate-local.ps1
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $root
if (-not (Test-Path .env)) { throw '缺少 .env' }

$compose = @('compose', '--project-directory', "$root", '--env-file', '.env',
             '-f', 'docker-compose.yml', '-f', 'docker-compose.archive.yml')
function Invoke-Compose {
    & docker @compose @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

Write-Host '启动归档库并等待就绪...'
Invoke-Compose up -d --no-deps --wait --wait-timeout 120 archive-postgres

$stage = '/tmp/ics-quality-migrate-local'
Invoke-Compose exec -T archive-postgres rm -rf $stage
Invoke-Compose cp 'quality/database/.' "archive-postgres:$stage"
try {
    Write-Host '执行迁移...'
    Invoke-Compose exec -T archive-postgres sh "$stage/apply-migrations.sh"
    Write-Host '校验表结构与权限...'
    Invoke-Compose exec -T archive-postgres sh "$stage/verify-schema.sh"
    Write-Host '已应用的迁移：'
    # Windows PowerShell 5 会吞掉传给外部程序参数里的双引号，SQL 改从标准输入传入。
    'SELECT version FROM ops.schema_migrations ORDER BY 1;' | & docker @compose exec -T archive-postgres sh -c 'psql -X -At -U $POSTGRES_USER -d $POSTGRES_DB'
    Write-Host '归档库迁移与校验通过。'
} finally {
    & docker @compose exec -T archive-postgres rm -rf $stage | Out-Null
}
