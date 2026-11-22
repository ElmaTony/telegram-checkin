"""Build a source ZIP from an explicit allowlist; exclude every runtime directory."""
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = ('VERSION', 'CHANGELOG.md', 'README.md', 'README_EN.md', 'LICENSE', '.gitignore',
         'app.py', 'telegram-checkin.spec', 'checkin.py', 'gui.py', 'gui.html', 'web_engine.py', 'requirements.txt', 'requirements-build.txt',
         'requirements.lock.txt', 'config.example.yaml', 'start_gui.bat', 'start_checkin.bat',
         'docs/CLOUD.md', 'docs/CONFIGURATION.md',
         'docs/assets/icon.svg', 'docs/assets/workbench.png', 'docs/assets/automation.png', 'docs/assets/topic-picker.png', 'docs/assets/manifest.json')
DIRECTORIES = ('core', 'static', 'tests', 'tools', 'deploy', '.github')
SUFFIXES = {'.py', '.js', '.css', '.html', '.svg', '.txt', '.md', '.yml', '.yaml', '.service', '.example', '.sh'}
SECRETS = re.compile(rb'\b(?:SCT[A-Za-z0-9]{16,}|sctp\d+t[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b')


def source_files(root=ROOT):
    root = root.resolve()
    result = [root/name for name in FILES if (root/name).is_file()]
    for name in DIRECTORIES:
        for path in (root/name).rglob('*'):
            if path.is_file() and path.suffix in SUFFIXES and '__pycache__' not in path.parts:
                result.append(path)
    for path in result:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('发布文件不能是符号链接或指向项目外部')
        if SECRETS.search(path.read_bytes()):
            raise ValueError(f'发布扫描发现疑似真实密钥：{path.relative_to(root)}，已阻止打包')
    return sorted(set(result))


def build(root=ROOT):
    root = root.resolve()
    version = (root/'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION 必须为 x.y.z')
    prefix = f'telegram-checkin-v{version}'
    directory = root/'dist'
    directory.mkdir(exist_ok=True)
    path = directory/f'{prefix}.zip'
    manifest = {}
    sources = source_files(root)
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        def add(name, data):
            info = ZipInfo(prefix+'/'+name, date_time=(2020,1,1,0,0,0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
        for source in sources:
            name = source.relative_to(root).as_posix()
            data = source.read_bytes()
            manifest[name] = hashlib.sha256(data).hexdigest()
            add(name, data)
        add('MANIFEST.json', json.dumps({'version':version,'sha256':manifest}, ensure_ascii=False, indent=2))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(f'{checksum}  {path.name}\n', encoding='ascii')
    return path


if __name__ == '__main__':
    print(build())
