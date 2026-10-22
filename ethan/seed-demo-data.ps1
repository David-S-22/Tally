[CmdletBinding()]
param(
    [switch]$SkipBudgetsReset,
    [switch]$SkipTransactions
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

function Wait-Url([string]$Url, [int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod $Url -TimeoutSec 5 | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "Timed out waiting for $Url"
}

$Transactions = @{
    "2026-08" = @(
        @(1, "Harbourview Realty", "Rent payment", 1100.00, 30),
        @(2, "FibreLink", "Internet plan", 79.00, 60),
        @(3, "Telco One", "Mobile plan", 49.00, 61),
        @(4, "Netflix", "Netflix.com subscription", 20.99, 33),
        @(4, "Woolworths", "Weekly groceries", 98.00, 81),
        @(8, "Coles", "Weekly groceries", 77.50, 81),
        @(15, "Aldi", "Groceries", 64.50, 81),
        @(22, "Woolworths", "Weekly groceries", 70.00, 81),
        @(3, "Cafe Sorella", "Brunch", 32.00, 80),
        @(14, "Thai Orchid", "Dinner", 58.00, 80),
        @(24, "Mr Wong", "Dinner", 50.00, 80),
        @(5, "Transport card", "Opal top-up", 38.50, 70),
        @(18, "Shell", "Fuel", 51.50, 70),
        @(7, "AGL Energy", "Electricity", 130.00, 62),
        @(19, "Sydney Water", "Water usage", 80.00, 62),
        @(11, "Spotify", "Spotify Premium", 12.99, 32),
        @(12, "Anytime Fitness Ultimo", "Direct debit membership fee", 17.50, 31)
    )
    "2026-09" = @(
        @(1, "Harbourview Realty", "Rent payment", 1100.00, 30),
        @(2, "FibreLink", "Internet plan", 79.00, 60),
        @(3, "Telco One", "Mobile plan", 49.00, 61),
        @(4, "Netflix", "Netflix.com subscription", 20.99, 33),
        @(3, "Woolworths", "Weekly groceries", 120.40, 81),
        @(9, "Coles", "Weekly groceries", 98.60, 81),
        @(14, "Aldi", "Groceries", 87.00, 81),
        @(20, "Woolworths", "Weekly groceries", 112.20, 81),
        @(27, "Harris Farm", "Fresh produce", 46.80, 81),
        @(2, "Cafe Sorella", "Brunch", 45.00, 80),
        @(6, "Thai Orchid", "Dinner", 68.50, 80),
        @(11, "Gelato Messina", "Dessert", 22.00, 80),
        @(19, "Bills Surry Hills", "Birthday dinner", 59.50, 80),
        @(23, "Uber Eats", "Takeaway", 39.00, 80),
        @(29, "Mr Wong", "Dinner", 41.00, 80),
        @(4, "Transport card", "Opal top-up", 38.50, 70),
        @(10, "Shell", "Fuel", 65.00, 70),
        @(17, "Transport card", "Opal top-up", 38.50, 70),
        @(26, "Uber", "Weekend trip rides", 50.00, 70),
        @(8, "AGL Energy", "Electricity", 140.00, 62),
        @(13, "Sydney Water", "Water usage", 90.00, 62),
        @(11, "Spotify", "Spotify Premium", 12.99, 32),
        @(12, "Anytime Fitness Ultimo", "Direct debit membership fee", 17.50, 31)
    )
    "2026-10" = @(
        @(1, "Harbourview Realty", "Rent payment", 1100.00, 30),
        @(2, "FibreLink", "Internet plan", 79.00, 60),
        @(3, "Telco One", "Mobile plan", 49.00, 61),
        @(4, "Netflix", "Netflix.com subscription", 20.99, 33),
        @(3, "Woolworths", "Weekly groceries", 142.30, 81),
        @(7, "Coles", "Weekly groceries", 96.85, 81),
        @(10, "Aldi", "Groceries", 74.20, 81),
        @(14, "Woolworths", "Weekly groceries", 131.45, 81),
        @(17, "Coles", "Groceries", 58.90, 81),
        @(19, "Harris Farm", "Fresh produce", 78.30, 81),
        @(2, "Cafe Sorella", "Brunch", 18.50, 80),
        @(4, "Thai Orchid", "Dinner", 62.00, 80),
        @(9, "Mr Wong", "Dinner", 48.50, 80),
        @(12, "Gelato Messina", "Dessert", 14.00, 80),
        @(15, "Uber Eats", "Takeaway", 36.50, 80),
        @(18, "Bills Surry Hills", "Brunch", 31.00, 80),
        @(21, "Subway", "Lunch", 23.50, 80),
        @(3, "Transport card", "Opal top-up", 38.50, 70),
        @(8, "Shell", "Fuel", 54.00, 70),
        @(14, "Transport card", "Opal top-up", 38.50, 70),
        @(6, "AGL Energy", "Electricity", 168.00, 62),
        @(13, "Sydney Water", "Water usage", 96.00, 62),
        @(11, "Spotify", "Spotify Premium", 12.99, 32),
        @(12, "Anytime Fitness Ultimo", "Direct debit membership fee", 17.50, 31),
        @(20, "ATM", "Cash withdrawal", 100.00, 1)
    )
}

if (-not $SkipBudgetsReset) {
    Write-Host "Resetting the Budgets database..."
    docker compose stop budgets-backend budgets-db | Out-Null
    docker compose rm -f budgets-backend budgets-db | Out-Null
    foreach ($volume in (docker volume ls -q --filter "name=budgets_data")) {
        docker volume rm $volume | Out-Null
    }
    docker compose up -d --build budgets-db budgets-backend budgets-frontend
    Wait-Url "http://localhost:5006/health"
}

if (-not $SkipTransactions) {
    Write-Host "Seeding transactions for 2026-08 to 2026-10..."
    docker compose up -d transactions-db | Out-Null
    Wait-Url "http://localhost:6001/health"

    $existing = @{}
    foreach ($row in (Invoke-RestMethod "http://localhost:6001/transactions")) {
        $existing["$(([string]$row.date).Substring(0, 10))|$($row.merchant)|$([decimal]$row.amount)"] = $true
    }

    $created = 0
    $skipped = 0
    foreach ($month in ($Transactions.Keys | Sort-Object)) {
        foreach ($entry in $Transactions[$month]) {
            $date = "$month-{0:D2}" -f $entry[0]
            $key = "$date|$($entry[1])|$([decimal]$entry[3])"
            if ($existing.ContainsKey($key)) {
                $skipped++
                continue
            }
            $body = @{
                date        = $date
                merchant    = $entry[1]
                description = $entry[2]
                amount      = $entry[3]
                category_id = $entry[4]
            } | ConvertTo-Json
            Invoke-RestMethod -Method Post "http://localhost:6001/transactions" -ContentType "application/json" -Body $body | Out-Null
            $existing[$key] = $true
            $created++
        }
    }
    Write-Host "Transactions created: $created, already present: $skipped"
}

docker compose restart budgets-frontend shared-frontend | Out-Null
Write-Host "Done. Open http://localhost:3000 and select October 2026."
