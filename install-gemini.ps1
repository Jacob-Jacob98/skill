$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$revision = 'f607fd4ed100ec4cf4e6c1f54d003e13e156e4bc'
$url = "https://codeload.github.com/Jacob-Jacob98/skill/zip/$revision"
$skillRoot = Join-Path $env:USERPROFILE '.gemini/skills'
[IO.Directory]::CreateDirectory($skillRoot) | Out-Null
$id = [guid]::NewGuid().ToString('N')
$temp = Join-Path ([IO.Path]::GetTempPath()) "wedding-skills-$id"
$lock = [Threading.Mutex]::new($false, "Local\JacobJacob98WeddingSkills-gemini")
$locked = $false
try {
    try { $locked = $lock.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) { throw 'Another install is already running.' }
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $zip = Join-Path $temp 'skills.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $temp, [Text.Encoding]::UTF8)
    $packages = @(Get-ChildItem -LiteralPath (Join-Path $temp "skill-$revision/gemini") -Directory)
    if ($packages.Count -ne 2) { throw 'Incomplete download: expected two skills.' }
    foreach ($pkg in $packages) {
        if (!(Test-Path -LiteralPath (Join-Path $pkg.FullName 'SKILL.md') -PathType Leaf)) { throw "Missing SKILL.md in $pkg" }
        $dest = Join-Path $skillRoot $pkg.Name
        if (Test-Path -LiteralPath $dest) {
            foreach ($file in (Get-ChildItem -LiteralPath $pkg.FullName -Recurse -File)) {
                $local = Join-Path $dest $file.FullName.Substring($pkg.FullName.Length + 1)
                if (!(Test-Path -LiteralPath $local -PathType Leaf) -or
                    (Get-FileHash -LiteralPath $local).Hash -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
                    throw "An existing skill differs. Back it up and move it out of this directory before retrying: $dest"
                }
            }
        }
    }
    foreach ($pkg in $packages) {
        $dest = Join-Path $skillRoot $pkg.Name
        if (!(Test-Path -LiteralPath $dest)) {
            $stage = Join-Path (Split-Path $skillRoot) ".wedding-skills-$id"
            try {
                Copy-Item -LiteralPath $pkg.FullName -Destination $stage -Recurse
                # On Windows a scanner can briefly lock newly copied files.
                for ($attempt = 1; ; $attempt++) {
                    try {
                        [IO.Directory]::Move($stage, $dest)
                        break
                    } catch [IO.IOException], [UnauthorizedAccessException] {
                        if ($attempt -ge 10 -or (Test-Path -LiteralPath $dest)) { throw }
                        Start-Sleep -Milliseconds (200 * $attempt)
                    }
                }
            } finally {
                if ((Test-Path -LiteralPath $stage) -and ([IO.Path]::GetFullPath($stage) -eq (Join-Path (Split-Path $skillRoot) ".wedding-skills-$id"))) {
                    Remove-Item -LiteralPath $stage -Recurse -Force
                }
            }
        }
        Write-Host "Skill available: $dest"
    }
    Write-Host 'Open Gemini CLI and run /skills reload, then /skills list.'
} finally {
    if ((Test-Path -LiteralPath $temp) -and ([IO.Path]::GetFullPath($temp) -eq (Join-Path ([IO.Path]::GetTempPath()) "wedding-skills-$id"))) {
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
    if ($locked) { $lock.ReleaseMutex() }
    $lock.Dispose()
}
