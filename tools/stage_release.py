"""Stage verified CI installers in an existing draft; never publish or overwrite."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.build_portable import TARGETS,artifact_paths,validate_artifact
from tools.publish_release import api_request,credential,upload_assets,verified_packages,remove_verified


def stage(api,root,repository,release_id,target,commit):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repository) or not isinstance(release_id,int) or release_id<=0:
        raise ValueError('仓库或草稿 ID 无效')
    if target not in TARGETS or not re.fullmatch(r'[0-9a-f]{40}',commit):
        raise ValueError('平台或提交无效')
    version=(root/'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('VERSION 无效')
    base=f'https://api.github.com/repos/{repository}/releases/{release_id}'
    release=api('GET',base)
    if (release.get('id')!=release_id or release.get('draft') is not True
            or release.get('tag_name')!='v'+version or release.get('target_commitish')!=commit):
        raise RuntimeError('只允许暂存到本版本、本次提交的既有草稿；禁止修改正式发行版')
    upload=urllib.parse.urlsplit(release['upload_url'].split('{')[0])
    if upload.scheme!='https' or upload.netloc!='uploads.github.com' or upload.path!=f'/repos/{repository}/releases/{release_id}/assets':
        raise RuntimeError('草稿上传地址无效')
    paths=artifact_paths(root/'dist',version,target)
    directory=(root/'dist').resolve()
    if (root/'dist').is_symlink() or not directory.is_relative_to(root.resolve()) or any(p.is_symlink() or p.resolve().parent!=directory for p in paths):
        raise ValueError('只允许上传本次 dist 中的平台包')
    validate_artifact(paths[0],version,target,commit)
    if paths[1].read_text(encoding='ascii').split()[0]!=hashlib.sha256(paths[0].read_bytes()).hexdigest():
        raise RuntimeError('平台包校验失败')
    allowed={p.name for t in TARGETS for p in artifact_paths(root/'dist',version,t)}
    allowed.update({f'telegram-checkin-v{version}.zip',f'telegram-checkin-v{version}.zip.sha256'})
    if any(a['name'] not in allowed for a in release.get('assets',[])):
        raise RuntimeError('草稿中存在非预期附件，未上传')
    # Other matrix jobs own their platform names; retain their verified uploads.
    names={p.name for p in paths}
    selected={**release,'assets':[a for a in release.get('assets',[]) if a['name'] in names]}
    upload_assets(api,selected,list(paths))
    confirmed=api('GET',base)
    if (confirmed.get('id')!=release_id or confirmed.get('draft') is not True
            or confirmed.get('tag_name')!='v'+version or confirmed.get('target_commitish')!=commit):
        raise RuntimeError('草稿状态变化，保留本地包')
    remove_verified(verified_packages(paths,confirmed['assets'],root=root))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True,choices=TARGETS)
    args=parser.parse_args()
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():
        raise RuntimeError('只能暂存已提交的干净源码构建')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    token=credential()
    def api(method,url,data=None,content_type='application/json'):
        req=api_request(method,url,data,{'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':content_type,'User-Agent':'telegram-checkin-draft-staging'})
        try:
            with urllib.request.urlopen(req,timeout=90) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'GitHub 暂存 HTTP {error.code}，未发布') from None
    stage(api,ROOT,os.environ.get('GITHUB_REPOSITORY',''),int(os.environ.get('RELEASE_DRAFT_ID','0')),args.target,commit)


if __name__=='__main__':main()
