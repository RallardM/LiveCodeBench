$ErrorActionPreference = 'Continue'
Set-Location 'B:\repos\MyProjects\_LiveCodeBench'
$env:HF_HUB_OFFLINE = '1'
& .venv\Scripts\python.exe lcb_bench.py --scenario code_execution --bundle 6 --random-sample 71 --workers 1 --max-tokens 16384 --eval-workers 8 --harness yes --harness-note http://127.0.0.1:3080 --name zz-bundle6-exec *> zz_bundle_exec6.out
"exec6 exit: $LASTEXITCODE $(Get-Date)" | Add-Content zz_bundle_exec6.out
