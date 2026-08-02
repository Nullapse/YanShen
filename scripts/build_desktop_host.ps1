param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$hostDirectory = Join-Path $repoRoot "desktop_host"
$hostSource = Join-Path $hostDirectory "Program.cs"
$hostExecutable = Join-Path $hostDirectory "gongkao_desktop_host.exe"
$hostManifest = Join-Path $hostDirectory "app.manifest"
$iconPath = Join-Path $repoRoot "assets\app-icon.ico"
$webviewLib = (& $PythonExecutable -c "from pathlib import Path; import webview; print(Path(webview.__file__).resolve().parent / 'lib')").Trim()

$compilerCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $compiler) {
    throw "The .NET Framework C# compiler was not found."
}

& $compiler `
    /nologo `
    /target:winexe `
    /platform:x64 `
    /optimize+ `
    "/win32icon:$iconPath" `
    "/win32manifest:$hostManifest" `
    "/out:$hostExecutable" `
    /reference:System.dll `
    /reference:System.Core.dll `
    /reference:System.Drawing.dll `
    /reference:System.Windows.Forms.dll `
    "/reference:$webviewLib\Microsoft.Web.WebView2.Core.dll" `
    "/reference:$webviewLib\Microsoft.Web.WebView2.WinForms.dll" `
    $hostSource

if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $hostExecutable)) {
    throw "Desktop host compilation failed."
}

Copy-Item -LiteralPath "$webviewLib\Microsoft.Web.WebView2.Core.dll" -Destination $hostDirectory -Force
Copy-Item -LiteralPath "$webviewLib\Microsoft.Web.WebView2.WinForms.dll" -Destination $hostDirectory -Force
Copy-Item -LiteralPath "$webviewLib\runtimes\win-x64\native\WebView2Loader.dll" -Destination $hostDirectory -Force

Write-Output "Desktop host built: $hostExecutable"
