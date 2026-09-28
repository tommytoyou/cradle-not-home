# Cradle Is Not Home

Private pipeline for one original space-motivation YouTube video per day.

**Roles:** Stakeholder Tom Erickson · Architect specs in `docs/handover/` · Claude AI developer · Claude Code coder.

## Start here

1. Read `docs/handover/00_README.md`
2. Claude AI: paste `docs/handover/06_PROMPT_CLAUDE_AI.md`
3. Claude Code: paste `docs/handover/07_PROMPT_CLAUDE_CODE.md`

## NASA B-roll spike

```bash
cd broll
pip install -r requirements.txt
python collect_nasa.py catalog --per-query 12
python collect_nasa.py download --limit 10 --media-type video
./cut_snippet.sh sources/FILE.mp4 00:01:00 00:01:08 01_launch launch_ignition_01
```

Do not commit `sources/`, `library/`, `.env`, or music.

NASA footage is public domain; do not imply NASA endorsement.
