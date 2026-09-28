#!/usr/bin/env python3
"""NASA Image and Video Library catalog + download. No API key."""
from __future__ import annotations
import argparse, csv, json, re, sys, time
from pathlib import Path
from urllib.parse import quote, urlparse
import requests, yaml

ROOT = Path(__file__).resolve().parent
LIBRARY, SOURCES = ROOT / "library", ROOT / "sources"
API = "https://images-api.nasa.gov"
S = requests.Session()
S.headers["User-Agent"] = "cradle-not-home-broll/1.0"
FOLDERS = ["01_launch","02_ascent","03_stage_sep","04_orbit_earth","05_eva","06_interior","07_mars","08_deep","09_control","10_failure_process","11_return","12_still_kb","_inbox"]

def folders():
    for n in FOLDERS:
        (LIBRARY / n).mkdir(parents=True, exist_ok=True)
    SOURCES.mkdir(exist_ok=True)

def get(url):
    for i in range(4):
        try:
            r = S.get(url, timeout=45); r.raise_for_status(); return r.json()
        except Exception as e:
            err = e; time.sleep(1.5*(i+1))
    raise RuntimeError(err)

def score(h):
    x = h.lower(); s = 0
    if x.endswith(".mp4"): s += 100
    elif x.endswith(".mov"): s += 80
    elif any(x.endswith(e) for e in (".jpg",".jpeg",".png")): s += 70
    else: return 0
    for t,p in (("~orig",50),("4k",40),("1080",25),("thumb",-80),(".srt",-200),("collection.json",-200)):
        if t in x: s += p
    return s

def best(nasa_id):
    items = get(f"{API}/asset/{quote(nasa_id)}").get("collection",{}).get("items",[])
    hrefs = [i.get("href","") for i in items]
    use = sorted([h for h in hrefs if score(h)>0], key=score, reverse=True)
    return use[0] if use else None, hrefs

def catalog(per_query=12, resolve=False):
    folders()
    queries = yaml.safe_load((ROOT/"queries.yaml").read_text())["queries"]
    seen, rows = set(), []
    for qi,q in enumerate(queries,1):
        media = q.get("media_type","video")
        print(f"[{qi}/{len(queries)}] {q['q']}")
        try:
            payload = get(f"{API}/search?q={quote(q['q'])}&media_type={media}&page=1&page_size={min(per_query,100)}")
        except Exception as e:
            print(" fail", e); continue
        for item in payload.get("collection",{}).get("items") or []:
            b = (item.get("data") or [{}])[0]
            nid = b.get("nasa_id")
            if not nid or nid in seen: continue
            seen.add(nid)
            rows.append({"nasa_id":nid,"folder":q["folder"],"moods":"|".join(q.get("moods") or []),
                "query":q["q"],"media_type":b.get("media_type"),"title":(b.get("title") or "").replace("\n"," ")[:120],
                "details_url":f"https://images.nasa.gov/details/{nid}","download_url":""})
            if sum(1 for r in rows if r["query"]==q["q"]) >= per_query: break
        time.sleep(0.2)
    if resolve:
        for i,r in enumerate(rows,1):
            try:
                u,_ = best(r["nasa_id"]); r["download_url"] = u or ""
            except Exception:
                pass
            if i%10==0: print(i, "resolved")
            time.sleep(0.12)
    fields = list(rows[0].keys()) if rows else ["nasa_id"]
    with (ROOT/"catalog.csv").open("w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    (ROOT/"catalog.json").write_text(json.dumps(rows, indent=2))
    print("wrote", len(rows), "items")

def download(limit=25, nasa_id=None, folder=None, media_type="video"):
    folders()
    if nasa_id:
        url,_ = best(nasa_id)
        if not url: sys.exit("no asset")
        targets = [{"nasa_id":nasa_id,"folder":folder or "_inbox","download_url":url,"title":nasa_id}]
    else:
        rows = json.loads((ROOT/"catalog.json").read_text())
        if not any(r.get("download_url") for r in rows):
            for r in rows:
                try:
                    u,_ = best(r["nasa_id"]); r["download_url"] = u or ""
                except Exception:
                    r["download_url"] = ""
                time.sleep(0.12)
            (ROOT/"catalog.json").write_text(json.dumps(rows, indent=2))
        targets = [r for r in rows if r.get("download_url")]
        if folder: targets = [r for r in targets if r.get("folder")==folder]
        if media_type: targets = [r for r in targets if r.get("media_type")==media_type]
        targets = targets[:limit]
    for i,r in enumerate(targets,1):
        url = r["download_url"]
        ext = Path(urlparse(url).path).suffix or ".mp4"
        name = re.sub(r"[^\w.-]+", "_", f"{r['folder']}_{r['nasa_id']}{ext}")[:100]
        dest = SOURCES / name
        if dest.exists() and dest.stat().st_size>10000:
            print("skip", dest.name); continue
        print(i, r["nasa_id"])
        with S.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix+".part")
            with tmp.open("wb") as f:
                for c in resp.iter_content(65536):
                    if c: f.write(c)
            tmp.replace(dest)

def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog"); c.add_argument("--per-query", type=int, default=12); c.add_argument("--resolve-assets", action="store_true")
    d = sub.add_parser("download"); d.add_argument("--limit", type=int, default=25); d.add_argument("--nasa-id"); d.add_argument("--folder"); d.add_argument("--media-type", default="video")
    sub.add_parser("folders")
    a = p.parse_args()
    if a.cmd=="folders": folders()
    elif a.cmd=="catalog": catalog(a.per_query, a.resolve_assets)
    else: download(a.limit, a.nasa_id, a.folder, a.media_type)

if __name__ == "__main__":
    main()
