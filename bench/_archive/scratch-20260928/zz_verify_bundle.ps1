$ErrorActionPreference = 'Continue'
Set-Location 'B:\repos\MyProjects\_LiveCodeBench'
$env:HF_HUB_OFFLINE = '1'
& .venv\Scripts\python.exe lcb_bench.py --scenario code_execution --bundle 4 --random-sample 40 --workers 1 --max-tokens 16384 --eval-workers 8 --harness yes --harness-note http://127.0.0.1:3080 --name zz-bundle4-exec *> zz_bundle_exec.out
"exec exit: $LASTEXITCODE $(Get-Date)" | Add-Content zz_bundle_exec.out
& .venv\Scripts\python.exe lcb_bench.py --scenario test_output_prediction --bundle 2 --random-sample 24 --workers 1 --max-tokens 16384 --eval-workers 8 --harness yes --harness-note http://127.0.0.1:3080 --name zz-bundle2-top *> zz_bundle_top.out
"top exit: $LASTEXITCODE $(Get-Date)" | Add-Content zz_bundle_top.out
