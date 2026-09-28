# Space B-roll library (NASA, real footage)

```bash
cd broll
python3 -m pip install -r requirements.txt
python3 collect_nasa.py folders
python3 collect_nasa.py catalog --per-query 12
python3 collect_nasa.py download --limit 25 --media-type video
./cut_snippet.sh sources/SOME.mp4 00:01:12 00:01:20 01_launch launch_sls_ignition_01
```

Credit every video: NASA public-domain mission footage, edited and graded. NASA does not endorse this channel.
