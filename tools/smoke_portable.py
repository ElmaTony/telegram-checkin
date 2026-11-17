"""Exercise a compiled program using only anonymous, temporary local data."""
import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main(binary):
    binary=Path(binary).resolve()
    for name in ('telegram-checkin.service','telegram-checkin-portable.service','telegram-checkin.env.example'):
        assert (binary.parent/'deploy'/name).is_file(), 'Missing deployment template: '+name
    expected=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    with tempfile.TemporaryDirectory(prefix='portable-smoke-') as directory:
        env={k:v for k,v in os.environ.items() if k not in ('CHECKIN_HOME','SERVERCHAN_SENDKEY','CHECKIN_GUI_TOKEN')}
        env.update(CHECKIN_HOME=directory,CHECKIN_NO_BROWSER='1',PYTHONUTF8='1')
        # Each CI platform installs Chromium in an explicit external cache first.
        result=subprocess.run([str(binary),'--version'],env=env,capture_output=True,text=True,encoding='utf-8',check=True,timeout=30)
        assert result.stdout.strip()==expected,result.stdout
        result=subprocess.run([str(binary),'--doctor','--browser'],env=env,capture_output=True,text=True,encoding='utf-8',check=True,timeout=60)
        report=json.loads(result.stdout);assert report['ok'] and not report['network_checked'],report
        (Path(directory)/'config.yaml').write_text('accounts: []\n',encoding='utf-8')
        invalid=subprocess.run([str(binary),'--login','anonymous-missing-account'],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        assert invalid.returncode!=0 and 'config.yaml' in invalid.stderr
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env.update(CHECKIN_GUI_HOST='127.0.0.1',CHECKIN_GUI_PORT=str(port))
        with (Path(directory)/'gui.log').open('w',encoding='utf-8') as stream:
            process=subprocess.Popen([str(binary)],env=env,stdout=stream,stderr=stream)
            try:
                deadline=time.monotonic()+30;base=f'http://127.0.0.1:{port}'
                while time.monotonic()<deadline:
                    if process.poll() is not None:raise RuntimeError('Compiled GUI exited')
                    try:
                        with urllib.request.urlopen(base+'/api/status',timeout=1) as response:
                            status=json.load(response)
                        break
                    except OSError:time.sleep(.2)
                else:raise RuntimeError('Compiled GUI did not start')
                assert not status['accounts'] and not status['busy']
                for path in ('/','/static/app.js','/static/workbench.js','/static/app.css'):
                    with urllib.request.urlopen(base+path,timeout=5) as response:assert response.status==200
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait(timeout=10)
    print(json.dumps({'passed':True,'version':expected,'browser_launch':True,'gui_resources':True,'login_dispatch':True,'telegram_calls':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary')
    main(parser.parse_args().binary)
