# Downloads the newest release asset of a GitHub project that matches a pattern (used by the SDR Pilot installer).
param([string]$Repo, [string]$Pattern, [string]$Out)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
  $r = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'SDR-Pilot-Setup' }
  $a = $r.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
  if (-not $a) { Write-Output "No download matching '$Pattern' in $Repo's latest release."; exit 2 }
  Write-Output ("Downloading {0} ({1} MB) - this can take a few minutes..." -f $a.name, [math]::Round($a.size / 1MB))
  Invoke-WebRequest -UseBasicParsing $a.browser_download_url -OutFile $Out
  Write-Output "Downloaded."
  exit 0
} catch {
  Write-Output "Download failed: $($_.Exception.Message)"
  exit 1
}
