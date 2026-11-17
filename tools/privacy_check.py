"""Reject runtime data, actual local identifiers, keys and unreviewed public images."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.package_release import source_files, SECRETS


def private_values(root=ROOT):
    values=set()
    if (root/'config.yaml').is_file():
        cfg=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8')) or {}
        for account in cfg.get('accounts',[]):
            values.add(str(account.get('name','')))
            for group in account.get('groups',[]):
                values.update(str(group.get(key,'')) for key in ('chat','title'))
    if (root/'data/secrets.json').is_file():
        secrets=json.loads((root/'data/secrets.json').read_text(encoding='utf-8'))
        values.update(value for value in secrets.values() if isinstance(value,str) and len(value)>12)
    return [value.encode() for value in values if len(value)>=4]


def findings(data, name, values):
    # The public repository URL and its existing author attribution are intentional;
    # they are not Telegram account configuration or a sample account identity.
    scrubbed=re.sub(rb'https://(?:api\.)?github\.com/[^\s)\]"<>]+',b'PUBLIC_REPOSITORY_URL',data)
    if name.endswith('LICENSE'):
        scrubbed=re.sub(rb'Copyright[^\n]*',b'PUBLIC_ATTRIBUTION',scrubbed)
    return bool(SECRETS.search(data) or any(value in scrubbed for value in values))


def check(root=ROOT, history=False, additional_private_values=()):
    values=[*private_values(root),*additional_private_values];files=source_files(root)
    issues=[]
    manifest=json.loads((root/'docs/assets/manifest.json').read_text(encoding='utf-8')) if (root/'docs/assets/manifest.json').exists() else {}
    for path in files:
        name=path.relative_to(root).as_posix();data=path.read_bytes()
        if path.suffix=='.png':
            if manifest.get(path.name,{}).get('sha256')!=hashlib.sha256(data).hexdigest():issues.append(name+': image is not in the reviewed manifest')
        elif findings(data,name,values):issues.append(name+': private identifier or key detected')
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
    for name in tracked:
        if name and (name.startswith(('audit/','data/','history/','logs/','images/')) or name in ('config.yaml','config.yaml.bak','state.json','state.json.bak')):
            issues.append(name+': runtime file tracked by Git')
    objects=0
    if history:
        rows=subprocess.check_output(['git','rev-list','--objects','HEAD','--branches','--tags'],cwd=root,text=True).splitlines()
        with subprocess.Popen(['git','cat-file','--batch'],cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE) as process:
            for row in rows:
                parts=row.split(' ',1);name=parts[1] if len(parts)>1 else ''
                process.stdin.write((parts[0]+'\n').encode());process.stdin.flush()
                header=process.stdout.readline().decode().split();data=process.stdout.read(int(header[2]));process.stdout.read(1);objects+=1
                if header[1] not in ('commit','tag','blob') or header[1]=='blob' and name.endswith(('.png','.jpg')):continue
                if findings(data,name,values):issues.append('history object '+parts[0][:12]+': private identifier or key detected')
            process.stdin.close();process.wait()
    if issues:raise RuntimeError('\n'.join(issues))
    return {'passed':True,'source_files':len(files),'history_objects':objects,'private_values_printed':False,'reviewed_images':len(manifest)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--history',action='store_true')
    print(json.dumps(check(history=parser.parse_args().history)))
