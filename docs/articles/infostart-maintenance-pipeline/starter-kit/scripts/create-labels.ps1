param(
    [Parameter(Mandatory = $true)]
    [string]$Repository
)

$labels = @(
    @{ Name = "approved"; Color = "1d76db"; Description = "Human approved implementation" },
    @{ Name = "changes-requested"; Color = "d93f0b"; Description = "Review returned the PR to FIX" },
    @{ Name = "decision:1"; Color = "0e8a16"; Description = "Human selected triage option 1" },
    @{ Name = "decision:2"; Color = "0e8a16"; Description = "Human selected triage option 2" },
    @{ Name = "decision:3"; Color = "0e8a16"; Description = "Human selected triage option 3" },
    @{ Name = "hold"; Color = "d93f0b"; Description = "Automation must not touch this item" },
    @{ Name = "in-work"; Color = "1d76db"; Description = "FIX opened a pull request" },
    @{ Name = "manual"; Color = "5319e7"; Description = "Work must be performed manually" },
    @{ Name = "needs-decision"; Color = "fbca04"; Description = "Human decision required" },
    @{ Name = "ready-fix"; Color = "0e8a16"; Description = "Reproduced obvious defect ready for FIX" },
    @{ Name = "reviewed"; Color = "0052cc"; Description = "Review passed; waiting for human ship" },
    @{ Name = "ship"; Color = "0e8a16"; Description = "Human authorized merge" }
)

$existing = @(gh label list --repo $Repository --limit 100 --json name --jq ".[].name")
if ($LASTEXITCODE -ne 0) {
    throw "Cannot list labels for $Repository"
}

foreach ($label in $labels) {
    if ($existing -contains $label.Name) {
        Write-Host "exists: $($label.Name)"
        continue
    }
    gh label create $label.Name --repo $Repository --color $label.Color --description $label.Description
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot create label $($label.Name)"
    }
    Write-Host "created: $($label.Name)"
}
