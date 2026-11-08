# End to end inside the image, from Windows PowerShell: the collector runs (read-only root, no capabilities) against a
# local web server, then reports are produced from the same volume. Needs Docker Desktop.
# Usage: .\tools\docker_e2e.ps1 [-Image ispq]
param([string]$Image = "ispq")
$ErrorActionPreference = "Stop"

$id = Get-Random
$vol = "ispq-e2e-$id"
$net = "ispq-e2e-net-$id"
$srv = "ispq-e2e-srv-$id"

function Invoke-Ispq {
    $out = docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --network $net -v "${vol}:/data" $Image @args
    if ($LASTEXITCODE -ne 0) { throw "ispq $args failed (exit $LASTEXITCODE)" }
    $out | Out-String
}
function Assert-Contains($text, $needle) { if ($text -notmatch [regex]::Escape($needle)) { throw "expected '$needle' in: $text" } }

try {
    docker network create $net | Out-Null
    docker volume create $vol | Out-Null
    # something to measure: a web server on the same network
    docker run -d --name $srv --network $net --entrypoint python $Image -m http.server 8000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not start the test server" }
    Start-Sleep -Seconds 2

    $conf = @"
interval = 5
database = "/data/ispq.db"
[[tcp]]
name = "web"
host = "$srv"
port = 8000
count = 3
gap = 0.05
[[http]]
name = "page"
url = "http://${srv}:8000/"
bandwidth = true
"@
    $conf | docker run --rm -i -v "${vol}:/data" --entrypoint sh $Image -c "cat > /data/ispq.toml"
    if ($LASTEXITCODE -ne 0) { throw "could not write the configuration" }

    Assert-Contains (Invoke-Ispq run --config /data/ispq.toml --cycles 3) "web 3/3"
    Assert-Contains (Invoke-Ispq status --config /data/ispq.toml) "cycles     3"
    Assert-Contains (Invoke-Ispq report --config /data/ispq.toml --format json) '"cycles": 3'
    Assert-Contains (Invoke-Ispq report --config /data/ispq.toml --format html) "<!doctype html>"
    Assert-Contains (Invoke-Ispq export-influx --config /data/ispq.toml) "ispq,kind=tcp,target=web "

    # something that is not there is a failure: exit code 1
    docker rm -f $srv | Out-Null
    docker run --rm --read-only --cap-drop ALL --tmpfs /tmp --network $net -v "${vol}:/data" $Image once --config /data/ispq.toml --no-save | Out-Null
    if ($LASTEXITCODE -ne 1) { throw "expected exit code 1 when the target is gone, got $LASTEXITCODE" }
    Write-Host "docker end-to-end: OK"
}
finally {
    docker rm -f $srv 2>$null | Out-Null
    docker volume rm -f $vol 2>$null | Out-Null
    docker network rm $net 2>$null | Out-Null
}
