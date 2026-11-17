"""Verify the native Linux service with anonymous data and a disposable systemd unit."""
import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main(binary):
    if sys.platform != 'linux':
        raise RuntimeError('This integration check requires a Linux systemd host')
    import grp
    binary=Path(binary).resolve()
    name='telegram-checkin-smoke-'+uuid.uuid4().hex+'.service'
    installed=Path('/run/systemd/system')/name
    assert installed.parent==Path('/run/systemd/system') and name.startswith('telegram-checkin-smoke-')
    def run(*args,check=True):
        result=subprocess.run(args,capture_output=True,text=True,timeout=120)
        if check and result.returncode:
            raise RuntimeError('Deployment command failed: '+result.stderr.strip())
        return result.stdout.strip()
    with tempfile.TemporaryDirectory(prefix='telegram-checkin-deployment-') as temporary:
        directory=Path(temporary);home=directory/'data';home.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        template=(binary.parent/'deploy/telegram-checkin-portable.service').read_text(encoding='utf-8')
        replacements={'User=telegram-checkin':'User='+getpass.getuser(),
                      'Group=telegram-checkin':'Group='+grp.getgrgid(os.getgid()).gr_name,
                      'WorkingDirectory=/opt/telegram-checkin':'WorkingDirectory='+str(binary.parent),
                      'Environment=CHECKIN_HOME=/var/lib/telegram-checkin':'Environment=CHECKIN_HOME='+str(home),
                      'StateDirectory=telegram-checkin':'StateDirectory=',
                      'EnvironmentFile=-/etc/telegram-checkin.env':'EnvironmentFile=',
                      'ExecStart=/opt/telegram-checkin/telegram-checkin --gui':'ExecStart='+str(binary)+' --gui'}
        for before,after in replacements.items():
            assert before in template,before
            template=template.replace(before,after)
        extra=['Environment=CHECKIN_GUI_HOST=127.0.0.1','Environment=CHECKIN_GUI_PORT='+str(port),
               'UnsetEnvironment=DISPLAY WAYLAND_DISPLAY SERVERCHAN_SENDKEY CHECKIN_GUI_TOKEN']
        if os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
            extra.append('Environment=PLAYWRIGHT_BROWSERS_PATH='+os.environ['PLAYWRIGHT_BROWSERS_PATH'])
        template=template.replace('[Install]','\n'.join(extra)+'\n\n[Install]')
        source=directory/name;source.write_text(template,encoding='utf-8')
        run('systemd-analyze','verify',str(source))
        def api(path,data=None):
            request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=3) as response:return json.load(response)
        def ready(recovered=False):
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                try:
                    status=api('/api/status')
                    if status['automation']['active'] and (not recovered or status['automation']['last_run'].get('status')=='interrupted'):
                        return status
                except (OSError,ValueError):pass
                time.sleep(.2)
            raise RuntimeError('Disposable systemd service did not become ready')
        try:
            run('sudo','install','-m','644',str(source),str(installed))
            run('sudo','systemctl','daemon-reload')
            run('sudo','systemctl','start',name)
            status=ready()
            assert status['accounts']==[] and not status['busy'] and not status['browser']['visible_available']
            automation=api('/api/automation')
            options={**automation['settings'],'enabled':False,'time':'23:47','timezone':'Asia/Hong_Kong'}
            assert api('/api/automation',{'schedule':options,'notifications':{'enabled':False,'only_on_issues':False},'revision':automation['revision']})['ok']
            config=api('/api/config');revision=config.pop('_revision')
            config['accounts']=[{'name':'Fixture','groups':[{'chat':'-101','title':'Fixture conversation','message':'签到'}]}]
            assert api('/api/config',{'cfg':config,'revision':revision})['ok']
            guard={'Fixture':{'-101':status['today']},'_last':{'Fixture':{'-101':{'date':status['today'],'confirm':'unconfirmed','sent':None,'stage':'awaiting_confirmation','actions':[]}}}}
            state=home/'state.json';temporary_state=state.with_suffix('.tmp')
            temporary_state.write_text(json.dumps(guard),encoding='utf-8');os.replace(temporary_state,state)
            runtime=home/'data/schedule-state.json'
            temporary_state=runtime.with_suffix('.tmp')
            temporary_state.write_text(json.dumps({'last_date':status['today'],'job_id':'fixture-interrupted','status':'running'}),encoding='utf-8')
            os.replace(temporary_state,runtime)
            run('sudo','systemctl','restart',name);ready(recovered=True)
            assert api('/api/automation')['settings']==options
            assert api('/api/automation')['last_run']['status']=='interrupted'
            group=api('/api/status')['accounts'][0]['groups'][0]
            assert group['done'] and group['confirm']=='unconfirmed' and group['sent'] is None
            old_pid=run('sudo','systemctl','show',name,'--property=MainPID','--value')
            run('sudo','systemctl','kill',name,'--kill-who=main','--signal=SIGKILL')
            time.sleep(.5);ready(recovered=True)
            assert run('sudo','systemctl','show',name,'--property=MainPID','--value')!=old_pid
            assert int(run('sudo','systemctl','show',name,'--property=NRestarts','--value'))>=1
            assert api('/api/automation')['settings']==options
            print(json.dumps({'passed':True,'systemd_start':True,'restart_persists_settings':True,'interrupted_job_preserved':True,'pending_dispatch_guard':True,'crash_recovery':True,'headless_service':True,'telegram_calls':0,'notifications_sent':0}))
        except Exception:
            print(run('sudo','journalctl','-u',name,'-n','60','--no-pager',check=False),file=sys.stderr)
            raise
        finally:
            run('sudo','systemctl','stop',name,check=False)
            run('sudo','rm','-f','--',str(installed))
            run('sudo','systemctl','daemon-reload')
            run('sudo','systemctl','reset-failed',name,check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary')
    main(parser.parse_args().binary)
