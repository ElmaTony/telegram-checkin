"""Persistent paths, frozen entry points and release safety without real accounts."""
import json
import io
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
import urllib.request

from core import paths
from tools import maintenance, privacy_check, publish_release, package_release, stage_release
from tools.build_portable import validate_artifact, copy_deployment_files, DEPLOY_FILES

ROOT=Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_privacy_scan_normalizes_relative_roots(self):
        (ROOT/'audit').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'audit') as directory:
            root=Path(directory);(root/'README.md').write_text('Anonymous fixture')
            subprocess.run(['git','init',str(root)],check=True,capture_output=True)
            relative=Path(os.path.relpath(root,Path.cwd()))
            self.assertTrue(privacy_check.check(relative)['passed'])

    def test_privacy_scan_can_add_local_patterns_without_copying_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'README.md').write_text('FixturePrivateIdentity')
            subprocess.run(['git','init',str(root)],check=True,capture_output=True)
            self.assertTrue(privacy_check.check(root)['passed'])
            with self.assertRaisesRegex(RuntimeError,'private identifier or key detected'):
                privacy_check.check(root,additional_private_values=[b'FixturePrivateIdentity'])

    def test_history_privacy_checks_commit_and_tag_identities(self):
        for kind in ('author','committer','tagger'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'VERSION').write_text('1.2.8')
                (root/'README.md').write_text('Anonymous source')
                (root/'config.yaml').write_text('accounts:\n- name: FixturePrivateIdentity\n  groups: []\n')
                env={**os.environ,'GIT_AUTHOR_NAME':'Contributors','GIT_AUTHOR_EMAIL':'contributors@example.invalid','GIT_COMMITTER_NAME':'Contributors','GIT_COMMITTER_EMAIL':'contributors@example.invalid'}
                def git(*args,settings=None):
                    return subprocess.run(['git',*args],cwd=root,env=settings or env,check=True,capture_output=True)
                git('init');git('add','VERSION','README.md');git('commit','-m','Safe fixture')
                self.assertTrue(privacy_check.check(root,history=True)['passed'])
                altered={**env,('GIT_AUTHOR_NAME' if kind=='author' else 'GIT_COMMITTER_NAME'):'FixturePrivateIdentity'}
                if kind=='tagger':git('tag','-a','fixture-tag','-m','Safe tag',settings=altered)
                else:git('commit','--allow-empty','-m','Safe fixture',settings=altered)
                with self.assertRaisesRegex(RuntimeError,'private identifier or key detected'):
                    privacy_check.check(root,history=True)

    def test_draft_staging_preserves_other_platforms_and_verifies_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'VERSION').write_text('1.2.6')
            archive,checksum=stage_release.artifact_paths(root/'dist','1.2.6','windows-x64')
            archive.write_bytes(b'fixture');checksum.write_text(hashlib.sha256(b'fixture').hexdigest()+'  '+archive.name)
            other={'name':'telegram-checkin-v1.2.6-linux-x64.tar.gz','state':'uploaded','size':3,'digest':'sha256:other'}
            release={'id':12,'draft':True,'tag_name':'v1.2.6','target_commitish':'a'*40,'upload_url':'https://uploads.github.com/repos/example/project/releases/12/assets{?name}','assets':[other]}
            methods=[]
            def api(method,url,data=None,content_type=None):
                methods.append(method)
                if method=='POST':release['assets'].append({'name':data.name,'state':'uploaded','size':data.stat().st_size,'digest':'sha256:'+hashlib.sha256(data.read_bytes()).hexdigest()})
                return release
            with patch.object(stage_release,'validate_artifact') as validate:
                stage_release.stage(api,root,'example/project',12,'windows-x64','a'*40)
            validate.assert_called_once_with(archive,'1.2.6','windows-x64','a'*40)
            self.assertEqual(methods.count('POST'),2);self.assertNotIn('PATCH',methods);self.assertNotIn('DELETE',methods)
            self.assertIn(other,release['assets']);self.assertTrue(release['draft']);self.assertFalse(archive.exists());self.assertFalse(checksum.exists())

    def test_draft_staging_rejects_published_wrong_commit_or_wrong_upload_host(self):
        base={'id':12,'draft':True,'tag_name':'v1.2.6','target_commitish':'a'*40,'upload_url':'https://uploads.github.com/repos/example/project/releases/12/assets','assets':[]}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'VERSION').write_text('1.2.6')
            for override in ({'draft':False},{'target_commitish':'b'*40},{'tag_name':'v1.2.5'},{'upload_url':'https://uploads.example.invalid/assets'}):
                methods=[]
                def api(method,*args):methods.append(method);return {**base,**override}
                with self.assertRaises(RuntimeError):stage_release.stage(api,root,'example/project',12,'windows-x64','a'*40)
                self.assertEqual(methods,['GET'])

    def test_draft_staging_keeps_local_files_when_remote_digest_does_not_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'VERSION').write_text('1.2.6')
            archive,checksum=stage_release.artifact_paths(root/'dist','1.2.6','windows-x64')
            archive.write_bytes(b'fixture');checksum.write_text(hashlib.sha256(b'fixture').hexdigest())
            release={'id':12,'draft':True,'tag_name':'v1.2.6','target_commitish':'a'*40,'upload_url':'https://uploads.github.com/repos/example/project/releases/12/assets','assets':[]}
            def api(method,url,data=None,content_type=None):
                if method=='POST':release['assets'].append({'name':data.name,'state':'uploaded','size':data.stat().st_size,'digest':'sha256:wrong'})
                return release
            with patch.object(stage_release,'validate_artifact'),self.assertRaisesRegex(RuntimeError,'远端附件未通过校验'):
                stage_release.stage(api,root,'example/project',12,'windows-x64','a'*40)
            self.assertTrue(archive.exists());self.assertTrue(checksum.exists());self.assertTrue(release['draft'])

    def test_file_upload_stream_has_exact_length_and_payload(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.length=self.headers.get('Content-Length')
                self.server.encoding=self.headers.get('Transfer-Encoding')
                self.server.body=self.rfile.read(int(self.server.length or 0))
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'fixture.zip';payload=b'fixture-data'*100000;path.write_bytes(payload)
                request=publish_release.api_request('POST',f'http://127.0.0.1:{server.server_port}/upload',path,{})
                self.assertNotIsInstance(request.data,bytes)
                with urllib.request.urlopen(request,timeout=5) as response:self.assertEqual(response.status,200)
                self.assertEqual(server.length,str(len(payload)))
                self.assertIsNone(server.encoding)
                self.assertEqual(server.body,payload)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_independent_release_assets_upload_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/f'fixture-{i}.zip' for i in range(3)]
            for path in paths:path.write_bytes(b'fixture')
            barrier=threading.Barrier(3);seen=[]
            def api(method,url,data,content_type):
                self.assertEqual(method,'POST');self.assertIsInstance(data,Path)
                barrier.wait(timeout=4);seen.append(data.name)
            publish_release.upload_assets(api,{'upload_url':'https://uploads.example.invalid/assets{?name}','assets':[]},paths)
            self.assertCountEqual(seen,[p.name for p in paths])

    def test_upload_preflight_and_failure_preserve_all_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/f'fixture-{i}.zip' for i in range(2)]
            for path in paths:path.write_bytes(b'fixture')
            calls=[]
            bad={'name':paths[-1].name,'state':'uploaded','size':7,'digest':'sha256:wrong'}
            with self.assertRaises(RuntimeError):
                publish_release.upload_assets(lambda *args:calls.append(args),{'upload_url':'https://uploads.example.invalid/assets','assets':[bad]},paths)
            self.assertEqual(calls,[])
            extra={'name':'unexpected.txt','state':'uploaded','size':7,'digest':'sha256:fixture'}
            with self.assertRaisesRegex(RuntimeError,'非预期附件'):
                publish_release.upload_assets(lambda *args:calls.append(args),{'upload_url':'https://uploads.example.invalid/assets','assets':[extra]},paths)
            self.assertEqual(calls,[])
            def failed(*args):raise RuntimeError('Upload interrupted')
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                publish_release.upload_assets(failed,{'upload_url':'https://uploads.example.invalid/assets','assets':[]},paths)
            self.assertTrue(all(path.is_file() for path in paths))

    def test_release_stays_draft_when_staged_asset_verification_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'docs'/'releases').mkdir(parents=True)
            (root/'VERSION').write_text('1.2.5')
            (root/'docs'/'releases'/'v1.2.5.md').write_text('# Project v1.2.5\n\nFixture changes')
            package=root/'dist'/'telegram-checkin-v1.2.5.zip';package.write_bytes(b'fixture')
            checksum=package.with_suffix('.zip.sha256');checksum.write_text('fixture checksum')
            assets=[];methods=[];fault='digest'
            def git(*args):
                if args==('remote','get-url','origin'):return 'https://github.com/example/project.git'
                if args==('rev-parse','HEAD'):return 'a'*40
                if args==('branch','--show-current'):return 'main'
                return ''
            def urlopen(request,**kwargs):
                method=request.get_method();methods.append(method);url=request.full_url
                if method=='GET' and '/releases?' in url:value=[]
                elif method=='POST' and 'uploads.' in url:
                    from urllib.parse import parse_qs,urlsplit
                    name=parse_qs(urlsplit(url).query)['name'][0]
                    data=b''.join(request.data)
                    digest='sha256:incorrect' if fault=='digest' else 'sha256:'+hashlib.sha256(data).hexdigest()
                    assets.append({'name':name,'state':'uploaded','size':len(data),'digest':digest})
                    value=assets[-1]
                else:value={'id':1,'draft':True,'tag_name':'v1.2.5','upload_url':'https://uploads.github.com/repos/example/project/releases/1/assets{?name}','assets':list(assets)}
                if method=='GET' and url.endswith('/releases/1') and fault=='unexpected':
                    value['assets'].append({'name':'unexpected.txt','state':'uploaded','size':7,'digest':'sha256:fixture'})
                return io.BytesIO(json.dumps(value).encode())
            with patch.object(publish_release,'ROOT',root),patch.object(publish_release,'build',return_value=package),patch.object(publish_release,'git',side_effect=git),patch.object(publish_release,'credential',return_value='fixture-token'),patch.object(privacy_check,'check',return_value={}),patch.object(publish_release.subprocess,'run'),patch.object(publish_release.urllib.request,'urlopen',side_effect=urlopen),patch.object(sys,'argv',['publish_release.py','--publish']):
                for fault in ('digest','unexpected'):
                    with self.subTest(fault=fault):
                        assets.clear();methods.clear()
                        with self.assertRaisesRegex(RuntimeError,'远端附件未通过校验|非预期附件'):publish_release.main()
                        self.assertNotIn('PATCH',methods)
                        self.assertTrue(package.exists() and checksum.exists())

    def test_linux_without_display_uses_background_browser(self):
        from web_engine import WebEngine
        with patch.object(sys,'platform','linux'),patch.dict(os.environ,{'DISPLAY':''}):
            self.assertFalse(paths.visible_browser_available())
            self.assertTrue(WebEngine({'name':'Fixture'},{}).headless)
        with patch.object(sys,'platform','linux'),patch.dict(os.environ,{'DISPLAY':':99'}):
            self.assertTrue(paths.visible_browser_available())

    def test_cloud_gui_reserves_background_job_and_explains_login_requirement(self):
        import gui
        with patch.object(gui,'visible_browser_available',return_value=False),patch.object(gui,'read_cfg',return_value={'accounts':[]}),patch.dict(gui.BOT,{'busy':False,'scanning':False,'job':None,'login_process':None}):
            gui._reserve('checkin',show_browser=True)
            self.assertFalse(gui.BOT['job']['show_browser'])
            gui.BOT.update(busy=False,job=None)
            with self.assertRaisesRegex(ValueError,'没有可视桌面'):gui._reserve('login')
            self.assertFalse(gui.BOT['busy'])

    def test_portable_bundle_contains_only_allowed_deployment_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'deploy').mkdir();folder=root/'app';folder.mkdir()
            for name in DEPLOY_FILES:(root/'deploy'/name).write_text('template')
            (root/'deploy'/'private.env').write_text('must not be copied')
            copy_deployment_files(folder,root)
            self.assertEqual({p.name for p in (folder/'deploy').iterdir()},set(DEPLOY_FILES))

    def test_uploaded_packages_are_all_removed_after_remote_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);dist=root/'dist';dist.mkdir()
            files=[dist/'telegram-checkin-v1.2.1.zip',dist/'telegram-checkin-v1.2.1-linux-arm64.tar.gz.sha256']
            for path in files:path.write_bytes(b'fixture')
            remote=[{'name':p.name,'state':'uploaded','size':p.stat().st_size,'digest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
            publish_release.remove_verified(publish_release.verified_packages(files,remote,root))
            self.assertEqual(list(dist.iterdir()),[])

    def test_missing_incomplete_or_mismatched_upload_preserves_all_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);dist=root/'dist';dist.mkdir()
            files=[dist/'telegram-checkin-v1.2.1.zip',dist/'telegram-checkin-v1.2.1.zip.sha256']
            for path in files:path.write_bytes(b'fixture')
            good=[{'name':p.name,'state':'uploaded','size':7,'digest':'sha256:'+hashlib.sha256(b'fixture').hexdigest()} for p in files]
            for override in ({'state':'starter'},{'size':8},{'digest':'sha256:incorrect'}):
                remote=[good[0],{**good[1],**override}]
                with self.assertRaises(RuntimeError):publish_release.verified_packages(files,remote,root)
                self.assertTrue(all(p.is_file() for p in files))
            with self.assertRaises(RuntimeError):publish_release.verified_packages(files,good[:1],root)
            self.assertTrue(all(p.is_file() for p in files))

    def test_release_cleanup_refuses_outside_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir()
            outside=root/'telegram-checkin-v1.2.1.zip';outside.write_bytes(b'fixture')
            with self.assertRaises(ValueError):publish_release.verified_packages([outside],[],root)
            self.assertTrue(outside.exists())

    def test_source_package_excludes_local_maintainer_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'docs'/'releases').mkdir(parents=True)
            for name in ('AGENTS.md','docs/RELEASING.md','docs/DEVELOPMENT.md','docs/releases/v1.2.1.md'):
                (root/name).write_text('Local maintenance notes')
            (root/'README.md').write_text('Usage')
            self.assertEqual([p.name for p in package_release.source_files(root)],['README.md'])

    def test_artifact_redirect_does_not_forward_github_credentials(self):
        with patch.object(sys,'path',[str(ROOT/'tools'),*sys.path]):
            from fetch_builds import ArtifactRedirect
        original=urllib.request.Request('https://api.github.com/repos/example/project/actions/artifacts/1/zip',headers={'Authorization':'Bearer example-token'})
        handler=ArtifactRedirect()
        redirected=handler.redirect_request(original,None,302,'Found',{},'https://artifacts.example.invalid/archive.zip?signature=example')
        self.assertIsNone(redirected.get_header('Authorization'))
        same_host=handler.redirect_request(original,None,302,'Found',{},'https://api.github.com/another-path')
        self.assertEqual(same_host.get_header('Authorization'),'Bearer example-token')

    def test_home_override_works_in_source_and_standalone(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CHECKIN_HOME':directory}):
            self.assertEqual(paths.data_home(),Path(directory).resolve())
            with patch.object(sys,'frozen',True,create=True):
                self.assertEqual(paths.data_home(),Path(directory).resolve())

    def test_frozen_login_reexecutes_program_not_a_missing_python_script(self):
        with patch.object(sys,'frozen',True,create=True):
            self.assertEqual(paths.login_command('Example'),[sys.executable,'--login','Example'])
        command=paths.login_command('Example')
        self.assertEqual(command[-2:],['login','Example'])
        self.assertEqual(Path(command[1]),ROOT/'web_engine.py')

    def test_source_assets_stay_available_with_external_data_home(self):
        with tempfile.TemporaryDirectory() as directory:
            script='import gui,json;from core import BASE_DIR;print(json.dumps({"home":str(BASE_DIR),"html":gui.GUI_HTML.is_file(),"static":gui.app.test_client().get("/static/app.js").status_code}))'
            result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env={**os.environ,'CHECKIN_HOME':directory},capture_output=True,text=True,check=True)
            data=json.loads(result.stdout)
            self.assertEqual(data['home'],str(Path(directory).resolve()))
            self.assertTrue(data['html']);self.assertEqual(data['static'],200)

    def test_cleanup_keeps_current_version_on_every_platform_and_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'VERSION').write_text('1.2.0')
            for name in ['telegram-checkin-v1.1.0.zip','telegram-checkin-v1.1.0.zip.sha256','telegram-checkin-v1.2.0.zip','telegram-checkin-v1.2.0-windows-x64.zip','telegram-checkin-v1.2.0-linux-arm64.tar.gz','telegram-checkin-v1.2.0-linux-arm64.tar.gz.sha256']:
                (root/'dist'/name).write_text('fixture')
            (root/'data').mkdir();(root/'data'/'secrets.json').write_text('{}')
            selected=maintenance.candidates(root)
            self.assertEqual({p.name for p,_ in selected},{'telegram-checkin-v1.1.0.zip','telegram-checkin-v1.1.0.zip.sha256'})
            maintenance.apply(selected,root)
            self.assertTrue((root/'data'/'secrets.json').is_file())
            self.assertTrue((root/'dist'/'telegram-checkin-v1.2.0-linux-arm64.tar.gz').is_file())

    def test_private_identifier_and_key_checks_do_not_print_values(self):
        self.assertTrue(privacy_check.findings(b'account=private-example','demo.py',[b'private-example']))
        self.assertTrue(privacy_check.findings(('SCT'+'x'*24).encode(),'demo.py',[]))
        self.assertFalse(privacy_check.findings(b'https://github.com/example/project','README.md',[b'example']))

    def test_native_package_must_match_clean_exact_source_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/'fixture.zip'
            def write(**extra):
                data={'version':'1.2.0','target':'windows-x64','commit':'source-commit','dirty':False,**extra}
                with ZipFile(archive,'w') as z:z.writestr('app/NATIVE-MANIFEST.json',json.dumps(data))
            write();validate_artifact(archive,'1.2.0','windows-x64','source-commit')
            for extra in ({'dirty':True},{'commit':'wrong'},{'target':'linux-x64'}):
                write(**extra)
                with self.assertRaises(ValueError):validate_artifact(archive,'1.2.0','windows-x64','source-commit')


if __name__=='__main__':unittest.main()
