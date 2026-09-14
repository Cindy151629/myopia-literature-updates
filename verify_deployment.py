"""External release verification and a persistent post-deployment receipt."""
import argparse, json, time, urllib.request, urllib.parse
from pathlib import Path
from updater import read, save, digest, now, safe_url, validate_record, HTTP

def verify(base,expected):
    if not safe_url(base):raise ValueError('Release URL must be HTTPS')
    def fetch(path):
        url=urllib.parse.urljoin(base.rstrip('/')+'/',path)
        if urllib.parse.urlsplit(url).netloc!=urllib.parse.urlsplit(base).netloc:raise ValueError('Snapshot host mismatch')
        req=urllib.request.Request(url+'?verify='+str(time.time_ns()),headers={'Origin':'null','Cache-Control':'no-cache','User-Agent':'MyopiaReleaseVerifier/1.0'})
        with urllib.request.urlopen(req,timeout=30,context=HTTP().context) as r:
            if r.headers.get('Access-Control-Allow-Origin')!='*':raise ValueError('Missing wildcard CORS needed by file://')
            body=r.read(40_000_001)
            if len(body)>40_000_000:raise ValueError('Oversize release')
            return body
    manifest=json.loads(fetch('manifest.json'))
    for k in ['domain_id','base_version','data_version','sha256','record_count','snapshot']:
        if manifest[k]!=expected[k]:raise ValueError('External version does not match candidate: '+k)
    if manifest['snapshot']!='snapshots/'+manifest['sha256']+'.json':raise ValueError('Invalid snapshot path')
    raw=fetch(manifest['snapshot'])
    if digest(raw)!=manifest['sha256']:raise ValueError('External digest mismatch')
    data=json.loads(raw)
    if data['domain_id']!=manifest['domain_id'] or data['base_version']!=manifest['base_version'] or len(data['records'])!=manifest['record_count']:raise ValueError('External schema mismatch')
    for p in data['records']:validate_record(p)
    return manifest

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('url');ap.add_argument('store');a=ap.parse_args();root=Path(a.store)
    expected=read(root/'public/manifest.json');error=None
    for attempt in range(6):
        try:verify(a.url,expected);error=None;break
        except Exception as e:error=str(e);time.sleep(5 if attempt<5 else 0)
    receipt=dict(schema_version=1,domain_id=expected['domain_id'],base_version=expected['base_version'],data_version=expected['data_version'],release_url=a.url,checked_at=now(),status='verified' if error is None else 'failed',error=error)
    save(root/'publication-status.json',receipt)
    if error:raise SystemExit('External release verification failed: '+error)
    state=read(root/'state/state.json');state['last_successful_publish']=receipt['checked_at'];state['verified_publication']=receipt;save(root/'state/state.json',state)
    print(json.dumps(receipt,ensure_ascii=False))
