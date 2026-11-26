"""Fetch all five successful native CI packages for this exact source commit."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from zipfile import ZipFile
from publish_release import credential, ROOT
from build_portable import TARGETS, artifact_paths, validate_artifact


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
        if urllib.parse.urlsplit(req.full_url).hostname != urllib.parse.urlsplit(newurl).hostname:
            redirected.remove_header('Authorization')
        return redirected


def _download_target(target, item, headers, version, head):
    opener=urllib.request.build_opener(ArtifactRedirect)
    buf=io.BytesIO()
    with opener.open(urllib.request.Request(item['archive_download_url'],headers=headers),timeout=120) as r:
        total=int(r.headers.get('Content-Length') or 0)
        got=reported=0
        while chunk:=r.read(256*1024):
            buf.write(chunk)
            got+=len(chunk)
            if got-reported>=16*1024*1024:
                print(f'Downloading {target}: {got}/{total or "?"}',flush=True)
                reported=got
    paths=artifact_paths(ROOT/'dist',version,target)
    buf.seek(0)
    with ZipFile(buf) as z:
        if set(z.namelist())!={p.name for p in paths}:raise ValueError('Unexpected build artifact files')
        for path in paths:path.write_bytes(z.read(path.name))
    if paths[1].read_text(encoding='ascii').split()[0]!=hashlib.sha256(paths[0].read_bytes()).hexdigest():raise ValueError('Build checksum mismatch')
    validate_artifact(paths[0],version,target,head)
    print(f'Verified {paths[0].name}',flush=True)
    return target, paths[0].name


def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    base='https://api.github.com/repos/ElmaTony/telegram-checkin'
    headers={'Authorization':'Bearer '+credential(),'Accept':'application/vnd.github+json','User-Agent':'telegram-checkin-build-fetch'}
    opener=urllib.request.build_opener(ArtifactRedirect)
    def get(url):
        with opener.open(urllib.request.Request(url,headers=headers),timeout=90) as r:
            return json.load(r)
    runs=get(base+'/actions/workflows/build.yml/runs?per_page=30&head_sha='+head)['workflow_runs']
    run=next((r for r in runs if r['head_sha']==head and r['conclusion']=='success'),None)
    if not run:raise RuntimeError('当前提交的五平台构建尚未全部成功；请查看 GitHub Actions 后重试')
    artifacts=get(run['artifacts_url']+'?per_page=100')['artifacts']
    items={}
    for target in TARGETS:
        item=next((a for a in artifacts if a['name']=='portable-'+target and not a['expired']),None)
        if not item:raise RuntimeError('缺少平台构建：'+target)
        items[target]=item
    (ROOT/'dist').mkdir(exist_ok=True)
    downloaded={}
    with ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
        futures={pool.submit(_download_target,t,items[t],headers,version,head):t for t in TARGETS}
        for fut in as_completed(futures):
            t, name=fut.result()
            downloaded[t]=name
    result=[downloaded[t] for t in TARGETS]
    print(json.dumps({'workflow':run['html_url'],'commit':head,'downloaded':result}))


if __name__=='__main__':main()
