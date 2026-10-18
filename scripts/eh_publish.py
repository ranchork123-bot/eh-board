#!/usr/bin/env python3
"""EH publish rail (GitHub Actions) - zero-cost publish path off the Oracle box.

Queue contract (Brain, Oct 18 2026):
  Sheet 1_Hh1lhXLBI8tO9uDKlsnBfGRAEMDWF8e5WmEN0A86d0
  Tabs: "Muse Queue" (FB), "Muse Queue IG" (IG), "Threads Queue"
  Statuses: draft|ready|posting|posted|held_review|failed
  Dispatchable: status=ready AND now(IST) inside [earliest_ist, latest_ist]
  Claim: status ready->posting (+ claim_ts cols M AND Q on Threads) then read
         back; proceed only if OUR claim holds. Fail closed on unknown claim age.
  Dedupe: dedupe_key + provider feed readback before every write.
  Success: posted + post_id + permalink (+ posted_at on Threads).
  Rows with queue_id starting TEST- are NEVER published (dry-run fixtures).
Arming: DRY_RUN=false in the workflow AND control cell ACTIVE_RAIL == "GHA".
Both required; default state publishes nothing.
"""
import os, sys, json, time, datetime
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GRequest

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SHEET_ID = os.environ["QUEUE_SHEET_ID"]
TOKEN = os.environ.get("META_TOKEN", "")
RUN_MARK = "gha:" + os.environ.get("GITHUB_RUN_ID", "local")
FB_PAGE = "296977197495150"
IG_USER = "17841406631455812"
THREADS_USER = os.environ.get("THREADS_USER_ID", "")
GV = "v23.0"
ACTIVE_RAIL_TAB = os.environ.get("ACTIVE_RAIL_TAB", "Config")
ACTIVE_RAIL_CELL = os.environ.get("ACTIVE_RAIL_CELL", "A1")  # VERIFY location with Brain
CLAIM_STALE_MIN = 30  # fail closed: never touch a 'posting' row older than this

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime.now(IST)

MUSE_COLS = ["queue_id","status","type","earliest_ist","latest_ist","audience","caption",
             "comment1","comment2","comment3","last_comment_marketing","feed_image_url",
             "story_image_url","hashtags_in_caption","source_note","dedupe_key",
             "preview_state","scheduled_at_ist","post_id","permalink"]
THREADS_COLS = ["queue_id","status","preview_state","earliest_ist","latest_ist","text",
                "mix_type","post_id","permalink","posted_at","views","likes","claim_ts",
                "reposts","last_metrics_at","notes","claim_ts2","image_url"]
TABS = {"Muse Queue": ("fb", MUSE_COLS), "Muse Queue IG": ("ig", MUSE_COLS),
        "Threads Queue": ("threads", THREADS_COLS)}

def sa_token():
    info = json.loads(os.environ["SA_JSON"])
    c = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    c.refresh(GRequest())
    return c.token

def sheets(method, path, body=None):
    r = requests.request(method, f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}",
                         headers={"Authorization": f"Bearer {sheets.tok}"}, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}

def get_rows(tab):
    d = sheets("GET", f"/values/{requests.utils.quote(tab)}")
    vals = d.get("values", [])
    return (vals[0] if vals else []), (vals[1:] if len(vals) > 1 else [])

def set_cell(tab, rownum, colidx, colname, value):
    col = chr(ord('A') + colidx)
    rng = f"{tab}!{col}{rownum}"  # decoded in body; encoded only in URL
    sheets("PUT", f"/values/{requests.utils.quote(rng)}?valueInputOption=RAW",
           {"range": rng, "values": [[value]]})

def graph(method, url, bearer_form=False, **kw):
    kw.setdefault("timeout", 60)
    if bearer_form:
        kw.setdefault("headers", {})["Authorization"] = f"OAuth {TOKEN}"
        r = requests.request(method, url, **kw)
    else:
        sep = "&" if "?" in url else "?"
        r = requests.request(method, f"{url}{sep}access_token={TOKEN}", **kw)
    try: body = r.json()
    except Exception: body = {"raw": r.text[:300]}
    return r.status_code, body

def in_window(row, cols):
    def p(s):
        s = (s or "").strip()
        for f in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M"):
            try: return datetime.datetime.strptime(s, f).replace(tzinfo=IST)
            except ValueError: pass
        return None
    e, l = p(row[cols.index("earliest_ist")]), p(row[cols.index("latest_ist")])
    if not e or not l: return False
    return e <= NOW <= l

def already_live(provider, dedupe_hint):
    """Provider feed readback: True => do NOT write again."""
    if not dedupe_hint: return False
    try:
        if provider == "fb":
            s, d = graph("GET", f"https://graph.facebook.com/{GV}/{FB_PAGE}/posts?fields=message,created_time&limit=20")
            items = d.get("data", [])
            return any(dedupe_hint in (p.get("message") or "") for p in items)
        if provider == "ig":
            s, d = graph("GET", f"https://graph.facebook.com/{GV}/{IG_USER}/media?fields=caption,timestamp&limit=20")
            return any(dedupe_hint in (m.get("caption") or "") for m in d.get("data", []))
        if provider == "threads":
            s, d = graph("GET", f"https://graph.threads.net/v1.0/{THREADS_USER}/threads?fields=text,timestamp&limit=20")
            return any(dedupe_hint in (t.get("text") or "") for t in d.get("data", []))
    except Exception as e:
        print(f"readback error {provider}: {e} - FAIL CLOSED")
        return True
    return False

def wait_container(cid, kind):
    for _ in range(40):
        time.sleep(10)
        if kind == "ig":
            s, d = graph("GET", f"https://graph.facebook.com/{GV}/{cid}?fields=status_code")
            if d.get("status_code") == "FINISHED": return True, d
            if d.get("status_code") == "ERROR": return False, d
        else:
            s, d = graph("GET", f"https://graph.threads.net/v1.0/{cid}?fields=status")
            if d.get("status") == "FINISHED": return True, d
            if "ERROR" in str(d.get("status", "")): return False, d
    return False, {"timeout": True}

def publish(provider, row, cols):
    g = lambda n: row[cols.index(n)] if n in cols and cols.index(n) < len(row) else ""
    if provider == "fb":
        typ, cap, url = g("type").lower(), g("caption"), g("feed_image_url")
        if typ == "text_only":
            return graph("POST", f"https://graph.facebook.com/{GV}/{FB_PAGE}/feed", data={"message": cap})
        if typ in ("text_photo", "multi_photo"):
            return graph("POST", f"https://graph.facebook.com/{GV}/{FB_PAGE}/photos", data={"url": url, "caption": cap})
        if typ == "video_reel":
            s, d = graph("POST", f"https://graph.facebook.com/{GV}/{FB_PAGE}/video_reels",
                         data={"upload_phase": "start"})
            up, vid = d.get("upload_url"), d.get("video_id")
            if not up or not vid: return s, d
            s2, d2 = graph("POST", up, bearer_form=True, data={"file_url": url})
            if s2 >= 500:
                s3, chk = graph("GET", f"https://graph.facebook.com/{GV}/{vid}?fields=status")
                if "ready" not in str(chk).lower() and "published" not in str(chk).lower():
                    return s2, {"transfer_error": d2, "reconcile": chk}
            return graph("POST", f"https://graph.facebook.com/{GV}/{FB_PAGE}/video_reels",
                         data={"upload_phase": "finish", "video_id": vid,
                               "video_state": "PUBLISHED", "description": cap})
        return 400, {"error": f"unknown fb type {typ}"}
    if provider == "ig":
        typ, cap, url = g("type").lower(), g("caption"), g("feed_image_url")
        if typ == "video_reel":
            payload = {"media_type": "REELS", "video_url": url, "caption": cap, "share_to_feed": "true"}
        elif typ in ("poster", "carousel"):
            payload = {"image_url": url, "caption": cap}
        else:
            return 400, {"error": f"unknown ig type {typ}"}
        s, d = graph("POST", f"https://graph.facebook.com/{GV}/{IG_USER}/media", data=payload)
        cid = d.get("id")
        if not cid: return s, d
        ok, st = wait_container(cid, "ig")
        if not ok: return 500, st
        return graph("POST", f"https://graph.facebook.com/{GV}/{IG_USER}/media_publish", data={"creation_id": cid})
    if provider == "threads":
        text, img = row[cols.index("text")], row[cols.index("image_url")]
        payload = {"media_type": "IMAGE" if img else "TEXT", "text": text}
        if img: payload["image_url"] = img
        s, d = graph("POST", f"https://graph.threads.net/v1.0/{THREADS_USER}/threads", data=payload)
        cid = d.get("id")
        if not cid: return s, d
        ok, st = wait_container(cid, "threads")
        if not ok: return 500, st
        return graph("POST", f"https://graph.threads.net/v1.0/{THREADS_USER}/threads_publish", data={"creation_id": cid})
    return 400, {"error": "unknown provider"}

def claim_stale(tab, rownum, row, cols):
    """Fail closed on unknown claim age for rows already 'posting'."""
    if "claim_ts" not in cols: return False
    ts = row[cols.index("claim_ts")]
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() > CLAIM_STALE_MIN * 60
    except Exception:
        return True  # unknown age -> fail closed

def main():
    if not os.environ.get("SA_JSON") or not TOKEN:
        print("secrets not configured yet; skipping")
        return 0
    sheets.tok = sa_token()
    if not DRY_RUN:
        rail = sheets("GET", f"/values/{requests.utils.quote(ACTIVE_RAIL_TAB + '!' + ACTIVE_RAIL_CELL)}")
        val = (rail.get("values") or [[""]])[0][0]
        if val != "GHA":
            print(f"active_rail cell = {val!r}, not GHA - not armed, exiting")
            return 0
    acted = 0
    for tab, (provider, cols) in TABS.items():
        hdr, rows = get_rows(tab)
        if not hdr: continue
        for i, row in enumerate(rows):
            rownum = i + 2
            row += [""] * (len(cols) - len(row))
            qid, status = row[0], row[1]
            if not qid: continue
            is_test = qid.startswith("TEST-")
            if not is_test:
                # No active_rail flag exists yet (Brain, Oct 18): Actions touches ONLY TEST- rows
                # until a flag cell is created and announced to Brain + Main.
                continue
            if status == "posting" and claim_stale(tab, rownum, row, cols):
                print(f"[{tab} r{rownum}] {qid} stale/unknown claim - FAIL CLOSED, skipping"); continue
            if status != "ready" or not in_window(row, cols): continue
            dedupe_hint = row[cols.index("dedupe_key")] if "dedupe_key" in cols else (row[cols.index("text")] if provider == "threads" else "")
            if already_live(provider, dedupe_hint):
                print(f"[{tab} r{rownum}] {qid} already live per provider readback - skip"); continue
            print(f"[{tab} r{rownum}] candidate {qid} provider={provider}")
            if DRY_RUN:
                print(f"[{tab} r{rownum}] DRY-RUN would claim+publish{' (TEST fixture: no publish ever)' if is_test else ''}"); continue
            # claim
            set_cell(tab, rownum, cols.index("status"), "status", "posting")
            if "claim_ts" in cols:
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                set_cell(tab, rownum, cols.index("claim_ts"), "claim_ts", ts)
                set_cell(tab, rownum, cols.index("claim_ts2"), "claim_ts2", ts)
            time.sleep(3)
            hdr2, rows2 = get_rows(tab)
            back = rows2[rownum - 2] + [""] * (len(cols) - len(rows2[rownum - 2]))
            if back[1] != "posting":
                print(f"[{tab} r{rownum}] claim lost for {qid} - skipping"); continue
            if is_test:
                print(f"[{tab} r{rownum}] TEST fixture {qid}: claim+readback OK, publish SKIPPED by design")
                set_cell(tab, rownum, cols.index("status"), "status", "ready")
                continue
            code, res = publish(provider, row, cols)
            pid = res.get("id") or res.get("post_id") or ""
            if code in (200, 201) and pid:
                set_cell(tab, rownum, cols.index("status"), "status", "posted")
                set_cell(tab, rownum, cols.index("post_id"), "post_id", str(pid))
                link = res.get("permalink") or (res.get("permalink_url") or "")
                if link: set_cell(tab, rownum, cols.index("permalink"), "permalink", str(link))
                if provider == "threads":
                    set_cell(tab, rownum, cols.index("posted_at"), "posted_at", NOW.isoformat())
                print(f"[{tab} r{rownum}] POSTED {qid} -> {pid}")
            else:
                set_cell(tab, rownum, cols.index("status"), "status", "ready")
                print(f"[{tab} r{rownum}] publish failed {qid} http={code} {str(res)[:200]}")
            acted += 1
    print(f"done. acted={acted} dry_run={DRY_RUN}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
