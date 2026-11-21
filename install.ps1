param(
    [Parameter(Mandatory)][ValidateSet('gemini', 'gpt')][string]$Platform,
    [switch]$SkillsOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$revision = 'f607fd4ed100ec4cf4e6c1f54d003e13e156e4bc'
$url = "https://codeload.github.com/Jacob-Jacob98/skill/zip/$revision"
$parent = if ($Platform -eq 'gemini') { '.gemini' } else { '.agents' }
$skillRoot = Join-Path (Join-Path $env:USERPROFILE $parent) 'skills'
[IO.Directory]::CreateDirectory($skillRoot) | Out-Null
$id = [guid]::NewGuid().ToString('N')
$temp = Join-Path ([IO.Path]::GetTempPath()) "wedding-skills-$id"
$lock = [Threading.Mutex]::new($false, "Local\JacobJacob98WeddingSkills-$Platform")
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
    $packages = @(Get-ChildItem -LiteralPath (Join-Path $temp "skill-$revision/$Platform") -Directory)
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
                [IO.Directory]::Move($stage, $dest)
            } finally {
                if ((Test-Path -LiteralPath $stage) -and ([IO.Path]::GetFullPath($stage) -eq (Join-Path (Split-Path $skillRoot) ".wedding-skills-$id"))) {
                    Remove-Item -LiteralPath $stage -Recurse -Force
                }
            }
        }
        Write-Host "Skill available: $dest"
    }
    if ($Platform -eq 'gpt' -and !$SkillsOnly) {
        $music = Join-Path $skillRoot 'wedding-music-workflow'
        $python = $null
        $opts = @()
        foreach ($candidate in @('py.exe', 'python.exe', (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'))) {
            $exe = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (!$exe -or $exe.Source -like '*\Microsoft\WindowsApps\python.exe') { continue }
            $opts = @()
            if ($candidate -eq 'py.exe') { $opts = @('-3.12') }
            $versionOK = & $exe.Source @opts -c 'import sys; print(int(sys.version_info >= (3,12)))'
            if ($LASTEXITCODE -eq 0 -and "$versionOK".Trim() -eq '1') { $python = $exe.Source; break }
        }
        if (!$python) { throw 'Skills installed. Install Python 3.12 for Windows and rerun this command to enable music processing.' }
        $wrapper = Join-Path $music 'scripts/runtime.py'
        $ready = $false
        if (Test-Path -LiteralPath (Join-Path $music 'runtime.json')) {
            & $python @opts -B $wrapper --doctor
            $ready = $LASTEXITCODE -eq 0
        }
        if (!$ready) {
            & $python @opts -B (Join-Path $music 'scripts/bootstrap.py')
            if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath (Join-Path $music 'runtime.json'))) { throw 'Skill files installed. Music runtime setup failed. Review the error above and rerun.' }
        }
    }
    if ($Platform -eq 'gemini') { Write-Host 'Open Gemini CLI and run /skills reload, then /skills list.' }
    else { Write-Host 'Open a new Codex chat and type $ to see installed skills.' }
} finally {
    if ((Test-Path -LiteralPath $temp) -and ([IO.Path]::GetFullPath($temp) -eq (Join-Path ([IO.Path]::GetTempPath()) "wedding-skills-$id"))) {
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
    if ($locked) { $lock.ReleaseMutex() }
    $lock.Dispose()
}
