import json, os, urllib.request, datetime, html

W = os.environ['WORKER'].rstrip('/')
K = os.environ['RAIL_KEY']

def get(path):
    r = urllib.request.Request(W + path, headers={'x-rail-key': K, 'User-Agent': 'curl/8.5.0'})
    with urllib.request.urlopen(r, timeout=20) as f:
        return json.load(f)

flags = get('/flags')
queue = get('/queue/peek?all=1').get('rows', [])
hbs = get('/heartbeats').get('rows', [])
now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

def esc(x): return html.escape(str(x)) if x is not None else ''

def postlink(note):
    # extract proof=<post_id> and build FB link
    if 'proof=' in (note or ''):
        pid = note.split('proof=')[1].split()[0]
        if '_' in pid:
            page, post = pid.split('_', 1)
            return '<a href="https://www.facebook.com/%s/posts/%s">%s</a>' % (page, post, esc(pid))
    return ''

qrows = ''.join(
    '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        esc(r.get('event_id')), esc(r.get('source')), esc((r.get('received_at') or '')[:16].replace('T',' ')),
        esc(r.get('status')), esc(r.get('claimed_by') or ''), esc((r.get('posted_at') or '')[:19].replace('T',' ')))
    for r in queue)

hrows = ''.join(
    '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        esc((h.get('ts') or '')[:19].replace('T',' ')), esc(h.get('rail')), esc(h.get('note')), postlink(h.get('note')))
    for h in hbs[:20])

page = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=300>
<title>EH Publish Rail</title>
<style>body{font:14px system-ui;background:#0f1115;color:#e6e6e6;max-width:900px;margin:20px auto;padding:0 12px}
table{border-collapse:collapse;width:100%%;margin:8px 0 24px}td,th{border:1px solid #333;padding:6px 8px;text-align:left}
th{background:#1a1d24}.flag{display:inline-block;padding:4px 10px;border-radius:6px;margin:2px 6px 2px 0;background:#1a1d24}
a{color:#7ab8ff}h1{font-size:20px}h2{font-size:16px;color:#9ab}</style>
<h1>Engage Hubtique - Publish Rail Board</h1>
<p>Updated %s (auto-refresh 5 min)</p>
<p><span class=flag>kill_switch: <b>%s</b></span><span class=flag>active_rail: <b>%s</b></span></p>
<h2>Queue</h2>
<table><tr><th>event</th><th>source</th><th>received UTC</th><th>status</th><th>claimed by</th><th>posted UTC</th></tr>%s</table>
<h2>Latest activity</h2>
<table><tr><th>time UTC</th><th>rail</th><th>note</th><th>post</th></tr>%s</table>
<p style=color:#9ab>To pause everything: message Instinct "stop rail".</p>
""" % (now, esc(flags.get('kill_switch','?')), esc(flags.get('active_rail','?')), qrows, hrows)

open('index.html','w').write(page)
print('board rendered', len(page))
