#!/usr/bin/env python3
"""EH publish rail (GitHub Actions).

Zero-cost publish path that bypasses the Oracle n8n box for the video/image
publish call. Meta fetches media from GitHub release assets (MEDIA_BASE),
never from Oracle.

Ownership model (dupe-safe with the n8n rail):
  A queue row belongs to exactly one rail via the `rail` column.
  rail == GHA  -> this script may claim it.
  anything else (empty, N8N, ...) -> never touched here.
  Claim = write status=PUBLISHING + run marker, wait, read back, and only
  proceed if OUR marker is present. The n8n rail must likewise skip
  rail == GHA rows before any cutover is armed (Brain/Muse side).

DRY_RUN=true (default, incl. cron until explicitly armed): reads the queue,
reports what it WOULD claim/publish, writes nothing, posts nothing.
"""
import os, sys, json, time
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GRequest

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SHEET_ID = os.environ["QUEUE_SHEET_ID"]
MEDIA_BASE = os.environ["MEDIA_BASE"].rstrip("/") + "/"
TOKEN = os.environ.get("META_TOKEN", "")
RUN_MARK = "gha:" + os.environ.get("GITHUB_RUN_ID", "local")

# Queue contract (VERIFY against Brain's caller before arming):
# tabs read in order; columns by header name on row 1.
TABS = ["Muse Queue", "Muse Queue IG"]
COL = {
    "key": "media.key", "status": "status", "rail": "rail",
    "caption": "caption", "target": "target", "permalink": "permalink",
    "run": "run.mark",
}
CLAIM_FROM = {"QUEUED", "queued", ""}

def sa_token():
    info = json.loads(os.environ["SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GRequest())
    return creds.token

def sheets(method, path, body=None):
    r = requests.request(method, f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}",
                         headers={"Authorization": f"Bearer {sheets.tok}"}, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}

def get_rows(tab):
    d = sheets("GET", f"/values/{requests.utils.quote(tab)}")
    vals = d.get("values", [])
    if not vals: return [], []
    hdr = vals[0]
    return hdr, vals[1:]

def col_idx(hdr, name):
    return hdr.index(name) if name in hdr else None

def set_cell(tab, rownum, hdr, colname, value):
    ci = col_idx(hdr, colname)
    if ci is None:
        raise RuntimeError(f"missing column {colname} in {tab}")
    col = chr(ord('A') + ci)
    # plain (decoded) range in the body; percent-encoded only in the URL path
    rng = f"{tab}!{col}{rownum}"
    sheets("PUT", f"/values/{requests.utils.quote(rng)}?valueInputOption=RAW",
           {"range": rng, "values": [[value]]})

def graph(method, url, **kw):
    kw.setdefault("timeout", 60)
    sep = "&" if "?" in url else "?"
    r = requests.request(method, f"{url}{sep}access_token={TOKEN}", **kw)
    return r.status_code, r.json() if r.content else {}

def publish(target, media_url, caption):
    """Graph publish. Endpoint params must match Brain's current caller contract."""
    if target == "fb-reel":
        page = os.environ["META_PAGE_ID"]
        return graph("POST", f"https://graph.facebook.com/v23.0/{page}/videos",
                     data={"file_url": media_url, "description": caption})
    if target == "ig-reel":
        ig = os.environ["META_IG_USER"]
        s, c = graph("POST", f"https://graph.facebook.com/v23.0/{ig}/media",
                     data={"media_type": "REELS", "video_url": media_url, "caption": caption})
        cid = c.get("id")
        if not cid: return s, c
        for _ in range(30):
            time.sleep(10)
            s, st = graph("GET", f"https://graph.facebook.com/v23.0/{cid}?fields=status_code")
            if st.get("status_code") == "FINISHED": break
        return graph("POST", f"https://graph.facebook.com/v23.0/{ig}/media_publish",
                     data={"creation_id": cid})
    raise RuntimeError(f"unknown target {target}")

def main():
    sheets.tok = sa_token()
    claimed = 0
    for tab in TABS:
        hdr, rows = get_rows(tab)
        if not hdr:
            print(f"[{tab}] empty"); continue
        missing = [c for c in COL.values() if c not in hdr]
        if missing:
            print(f"[{tab}] MISSING COLUMNS {missing} - queue contract mismatch, skipping tab"); continue
        for i, row in enumerate(rows):
            rownum = i + 2
            cell = lambda name: row[col_idx(hdr, COL[name])] if col_idx(hdr, COL[name]) is not None and col_idx(hdr, COL[name]) < len(row) else ""
            status, rail, key, target = cell("status"), cell("rail"), cell("key"), cell("target")
            if rail != "GHA" or status not in CLAIM_FROM or not key:
                continue
            media_url = MEDIA_BASE + key
            print(f"[{tab} r{rownum}] candidate key={key} target={target}")
            if DRY_RUN:
                print(f"[{tab} r{rownum}] DRY-RUN would claim+publish {media_url}")
                continue
            set_cell(tab, rownum, hdr, "status", "PUBLISHING")
            set_cell(tab, rownum, hdr, "run", RUN_MARK)
            time.sleep(3)
            hdr2, rows2 = get_rows(tab)
            back = rows2[rownum - 2]
            if back[col_idx(hdr2, COL["run"])] != RUN_MARK:
                print(f"[{tab} r{rownum}] claim LOST (another rail won) - skipping"); continue
            code, res = publish(target, media_url, cell("caption"))
            if code in (200, 201) and (res.get("id") or res.get("post_id")):
                link = res.get("permalink") or res.get("id") or res.get("post_id")
                set_cell(tab, rownum, hdr, "status", "POSTED")
                set_cell(tab, rownum, hdr, "permalink", str(link))
                print(f"[{tab} r{rownum}] POSTED {link}")
            else:
                set_cell(tab, rownum, hdr, "status", "QUEUED")
                print(f"[{tab} r{rownum}] publish failed http={code} err={str(res)[:200]}")
            claimed += 1
    print(f"done. claimed={claimed} dry_run={DRY_RUN}")

if __name__ == "__main__":
    sys.exit(main())
