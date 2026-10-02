# Space B-roll library (NASA, real footage)

```bash
cd broll
python3 -m pip install -r requirements.txt
python3 collect_nasa.py folders
python3 collect_nasa.py catalog --per-query 12
python3 collect_nasa.py download --limit 25 --media-type video
./cut_snippet.sh sources/SOME.mp4 00:01:12 00:01:20 01_launch launch_sls_ignition_01
```

## Autocut and review (Windows PowerShell, from the repo root)

```powershell
python -m broll.autocut                      # cuts new files in broll\sources into library\_inbox
Start-Process library\_inbox\review.html     # keep/reject, folder, tags; then "Export decisions"
python -m broll.apply_review "$HOME\Downloads\review_decisions.json"
```

`apply_review` moves kept clips into their folders, deletes rejects and updates
`data\snippets.csv` (with `nasa_id` and credit). Undecided clips stay in the
inbox; rerun `python -m broll.autocut --page-only` to rebuild the page.

Credit every video: NASA public-domain mission footage, edited and graded. NASA does not endorse this channel.
