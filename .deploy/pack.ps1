$ErrorActionPreference = 'Stop'
$Root = 'c:\Users\HP\Desktop\poolbetting-main'
$OutDir = Join-Path $Root '.deploy'
if (!(Test-Path $OutDir)) { New-Item -ItemType Directory $OutDir -Force | Out-Null }

$files = @(
  'betting\migrations\0107_add_siteconfig_withdrawals_pause_controls.py',
  'betting\admin.py',
  'betting\models.py',
  'betting\views.py'
)

foreach ($rel in $files) {
  $path = Join-Path $Root $rel
  $bytes = [IO.File]::ReadAllBytes($path)
  $ms = New-Object IO.MemoryStream
  $gz = New-Object IO.Compression.GzipStream($ms, [IO.Compression.CompressionLevel]::Optimal, $false)
  $gz.Write($bytes, 0, $bytes.Length)
  $gz.Close()
  $compressed = $ms.ToArray()
  $b64 = [Convert]::ToBase64String($compressed, [Base64FormattingOptions]::None)
  $safeName = ($rel -replace '[\\/]', '_') + '.gz.b64'
  $out = Join-Path $OutDir $safeName
  [IO.File]::WriteAllText($out, $b64, [Text.Encoding]::ASCII)
  $parts = @()
  $partSize = 30000
  $idx = 0
  for ($i = 0; $i -lt $b64.Length; $i += $partSize) {
    $len = [Math]::Min($partSize, $b64.Length - $i)
    $chunk = $b64.Substring($i, $len)
    $idx++
    $partName = $safeName + '.part' + $idx
    [IO.File]::WriteAllText((Join-Path $OutDir $partName), $chunk, [Text.Encoding]::ASCII)
    $parts += $partName
  }
  Write-Host ("===")
  Write-Host ("FILE        : {0}" -f $rel)
  Write-Host ("  raw bytes : {0}" -f $bytes.Length)
  Write-Host ("  gz bytes  : {0}" -f $compressed.Length)
  Write-Host ("  b64 chars : {0}" -f $b64.Length)
  Write-Host ("  parts (30k): {0} -> {1}" -f $parts.Count, ($parts -join ' '))
}
