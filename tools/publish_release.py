"""Publish a reviewed, committed version using existing GitHub credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_release import build
from tools.build_portable import TARGETS, artifact_paths, validate_artifact

PACKAGE_NAME = re.compile(r'telegram-checkin-v(\d+\.\d+\.\d+)(?:-(?:windows|linux|macos)-(?:x64|arm64))?\.(?:zip|tar\.gz)(?:\.sha256)?')


def verified_packages(assets, remote, root=ROOT):
    """Validate all files before deleting any; never follow a link out of dist."""
    directory = (root/'dist').resolve()
    if (root/'dist').is_symlink() or not directory.is_relative_to(root.resolve()):
        raise ValueError('发行目录不能是符号链接或位于项目外')
    by_name = {asset['name']: asset for asset in remote}
    verified = []
    for path in assets:
        if path.is_symlink() or path.resolve().parent != directory or not PACKAGE_NAME.fullmatch(path.name):
            raise ValueError('清理仅允许 dist 中已识别的发行附件')
        data = path.read_bytes()
        asset = by_name.get(path.name, {})
        if (asset.get('state') != 'uploaded' or asset.get('size') != len(data)
                or asset.get('digest') != 'sha256:'+hashlib.sha256(data).hexdigest()):
            raise RuntimeError('远端附件未通过校验，保留全部本地包：'+path.name)
        verified.append(path)
    return verified


def remove_verified(assets):
    for path in assets:
        path.unlink()
    print(f'远端校验通过，已删除 {len(assets)} 个本地发行附件')


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def credential():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        result = subprocess.run(['git','credential','fill'], input='protocol=https\nhost=github.com\n\n', text=True, capture_output=True, timeout=30,
                                env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'})
        values = dict(row.split('=',1) for row in result.stdout.splitlines() if '=' in row)
        token = values.get('password')
    if not token:
        raise RuntimeError('未找到 GitHub 发布凭据，请配置 git credential 或 GH_TOKEN')
    return token


def upload_chunks(path):
    sent = reported = 0
    with path.open('rb') as stream:
        while block := stream.read(256 * 1024):
            yield block
            sent += len(block)
            if sent - reported >= 8 * 1024 * 1024:
                print(f'Uploading {path.name}: {sent}/{path.stat().st_size}', flush=True)
                reported = sent


def api_request(method, url, data, headers):
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False).encode()
    elif isinstance(data, Path):
        payload = upload_chunks(data)
    else:
        payload = data
    request = urllib.request.Request(url, data=payload, method=method, headers=headers)
    if isinstance(data, Path):
        request.add_header('Content-Length', str(data.stat().st_size))
    return request


def upload_assets(api, release, assets):
    if len({path.name for path in assets}) != len(assets):
        raise ValueError('发行附件名称不能重复')
    uploaded = {item['name']: item for item in release.get('assets', [])}
    if set(uploaded) - {path.name for path in assets}:
        raise RuntimeError('草稿中存在非预期附件，已停止，未发布或覆盖附件')
    pending = []
    for path in assets:
        existing = uploaded.get(path.name)
        if existing:
            if (existing.get('state') != 'uploaded' or existing.get('size') != path.stat().st_size
                    or existing.get('digest') != 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()):
                raise RuntimeError('草稿版本存在未完成或内容不同的同名附件，已停止，未覆盖附件')
        else:
            pending.append(path)

    def upload(path):
        url = release['upload_url'].split('{')[0] + '?' + urllib.parse.urlencode({'name': path.name})
        api('POST', url, path, 'application/octet-stream' if path.name.endswith(('.zip', '.tar.gz')) else 'text/plain')

    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(upload, path) for path in pending]
        for task in as_completed(tasks):
            task.result()  # A failed upload leaves the release as a draft and all files intact.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish',action='store_true',help='push current branch/tag, upload assets and publish GitHub release')
    parser.add_argument('--with-portable',action='store_true',help='require and attach the five verified native CI packages')
    parser.add_argument('--cleanup-published',action='store_true',help='verify uploaded release assets and remove all matching local packages')
    args=parser.parse_args()
    if args.cleanup_published and (args.publish or args.with_portable):
        parser.error('--cleanup-published 请单独使用')
    if not args.cleanup_published:
        from tools.privacy_check import check
        check(history=True)
        package=build()
        if not args.publish:
            print('已打包：'+str(package)+'；发布时增加 --publish')
            return
        if git('status','--porcelain'):
            raise RuntimeError('存在未提交修改；请完成验证并提交本次源代码后发布')
    match=re.fullmatch(r'https://github.com/([\w.-]+/[\w.-]+?)(?:\.git)?',git('remote','get-url','origin'))
    if not match:
        raise RuntimeError('origin 必须指向明确的 GitHub HTTPS 仓库')
    repository=match[1]
    token=credential()

    def api(method, url, data=None, content_type='application/json'):
        req=api_request(method, url, data, {'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':content_type,'User-Agent':'telegram-checkin-release','X-GitHub-Api-Version':'2022-11-28'})
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'GitHub API HTTP {error.code}，发布未完成') from None

    base=f'https://api.github.com/repos/{repository}'
    if args.cleanup_published:
        groups={}
        for path in sorted((ROOT/'dist').glob('telegram-checkin-v*')):
            match=PACKAGE_NAME.fullmatch(path.name)
            if match:
                groups.setdefault(match[1],[]).append(path)
        verified=[]
        for version,files in groups.items():
            release=api('GET',base+'/releases/tags/v'+version)
            if release['draft']:
                raise RuntimeError('草稿尚未发布，保留本地包')
            verified.extend(verified_packages(files,release['assets'],root=ROOT))
        remove_verified(verified)
        return
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip();tag='v'+version
    repo_title=' '.join(part.capitalize() for part in re.split(r'[-_]+',repository.split('/')[-1]) if part)
    release_title=f'{repo_title} {tag}'
    notes=(ROOT/'docs'/'releases'/f'{tag}.md').read_text(encoding='utf-8')
    if not notes.startswith(f'# {release_title}'):
        raise RuntimeError(f'发行说明标题必须以 "# {release_title}" 开头')
    releases=api('GET',base+'/releases?per_page=100')
    existing=next((item for item in releases if item['tag_name']==tag),None)
    if existing and not existing['draft']:
        raise RuntimeError('该版本已经发布，禁止覆盖；请增加 VERSION 并更新发布说明')
    commit=git('rev-parse','HEAD');branch=git('branch','--show-current')
    assets=[package,package.with_suffix('.zip.sha256')]
    if args.with_portable:
        for target in TARGETS:
            archive,checksum=artifact_paths(ROOT/'dist',version,target)
            validate_artifact(archive,version,target,commit)
            if checksum.read_text(encoding='ascii').split()[0]!=hashlib.sha256(archive.read_bytes()).hexdigest():
                raise RuntimeError('平台包校验失败：'+target)
            assets.extend([archive,checksum])
    if not branch:
        raise RuntimeError('当前为 detached HEAD，请先切换到发布分支')
    subprocess.run(['git','push','origin',branch],cwd=ROOT,check=True)
    tags=git('tag','--list',tag)
    if tags:
        if git('rev-list','-n','1',tag)!=commit:
            raise RuntimeError('版本标签指向其他提交，禁止覆盖')
    else:
        subprocess.run(['git','tag','-a',tag,'-m',f'Release {tag}'],cwd=ROOT,check=True)
    subprocess.run(['git','push','origin',tag],cwd=ROOT,check=True)
    release=existing or api('POST',base+'/releases',{'tag_name':tag,'target_commitish':commit,'name':release_title,'body':notes,'draft':True,'prerelease':False})
    upload_assets(api, release, assets)
    staged=api('GET',base+'/releases/'+str(release['id']))
    if {item['name'] for item in staged['assets']} != {path.name for path in assets}:
        raise RuntimeError('草稿中存在非预期附件，已停止，未正式发布')
    verified_packages(assets,staged['assets'],root=ROOT)
    published=api('PATCH',base+'/releases/'+str(release['id']),{'draft':False,'name':release_title,'body':notes})
    confirmed=api('GET',base+'/releases/'+str(published['id']))
    if confirmed['draft'] or confirmed['tag_name']!=tag:
        raise RuntimeError('发行版尚未确认，保留本地包')
    remove_verified(verified_packages(assets,confirmed['assets'],root=ROOT))
    print(published['html_url'])


if __name__=='__main__':
    main()
