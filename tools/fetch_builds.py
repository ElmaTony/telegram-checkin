"""Fetch all five successful native CI packages for this exact source commit."""
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


def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    base='https://api.github.com/repos/ElmaTony/telegram-checkin'
    headers={'Authorization':'Bearer '+credential(),'Accept':'application/vnd.github+json','User-Agent':'telegram-checkin-build-fetch'}
    opener=urllib.request.build_opener(ArtifactRedirect)
    def get(url,raw=False):
        with opener.open(urllib.request.Request(url,headers=headers),timeout=90) as r:
            return r.read() if raw else json.load(r)
    runs=get(base+'/actions/workflows/build.yml/runs?per_page=30&head_sha='+head)['workflow_runs']
    run=next((r for r in runs if r['head_sha']==head and r['conclusion']=='success'),None)
    if not run:raise RuntimeError('当前提交的五平台构建尚未全部成功；请查看 GitHub Actions 后重试')
    artifacts=get(run['artifacts_url']+'?per_page=100')['artifacts']
    result=[];(ROOT/'dist').mkdir(exist_ok=True)
    for target in TARGETS:
        item=next((a for a in artifacts if a['name']=='portable-'+target and not a['expired']),None)
        if not item:raise RuntimeError('缺少平台构建：'+target)
        paths=artifact_paths(ROOT/'dist',version,target)
        with ZipFile(io.BytesIO(get(item['archive_download_url'],raw=True))) as z:
            if set(z.namelist())!={p.name for p in paths}:raise ValueError('Unexpected build artifact files')
            for path in paths:path.write_bytes(z.read(path.name))
        if paths[1].read_text(encoding='ascii').split()[0]!=hashlib.sha256(paths[0].read_bytes()).hexdigest():raise ValueError('Build checksum mismatch')
        validate_artifact(paths[0],version,target,head)
        result.append(paths[0].name)
    print(json.dumps({'workflow':run['html_url'],'commit':head,'downloaded':result}))


if __name__=='__main__':main()
