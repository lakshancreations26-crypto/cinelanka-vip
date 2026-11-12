$ErrorActionPreference = "Stop"

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
$server = Join-Path $PSScriptRoot "server.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python 3.13 was not found at $python. Install Python 3.13 and run this script again."
}
if (-not (Test-Path -LiteralPath $server)) {
    throw "Website server file was not found: $server"
}

$securePassword = Read-Host "Enter a new or existing admin password (at least 14 characters)" -AsSecureString
$passwordLength = $securePassword.Length
if ($passwordLength -lt 14) {
    throw "The admin password must contain at least 14 characters."
}

$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $env:CINELANKA_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    & $python $server
    if ($LASTEXITCODE -ne 0) {
        throw "CineLanka VIP stopped with exit code $LASTEXITCODE."
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    Remove-Item Env:\CINELANKA_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}
