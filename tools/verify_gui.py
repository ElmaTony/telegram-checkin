"""Run intercepted GUI checks against an isolated, disposable application service."""
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    (ROOT/'audit').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gui-fixture-',dir=ROOT/'audit') as directory:
        env={k:v for k,v in os.environ.items() if k not in ('SERVERCHAN_SENDKEY','CHECKIN_GUI_TOKEN')}
        env.update(CHECKIN_HOME=directory,CHECKIN_GUI_HOST='127.0.0.1',CHECKIN_GUI_PORT=str(port),CHECKIN_NO_BROWSER='1',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        env['CHECKIN_TEST_URL']=f'http://127.0.0.1:{port}'
        # Run the production Flask application in this process. On Windows the
        # venv Python redirector may leave a child server alive after termination.
        os.environ.update(env)
        sys.path.insert(0,str(ROOT))
        import threading
        from gui import app
        from waitress import create_server
        server=create_server(app,host='127.0.0.1',port=port)
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for name in ('browser_ui.py','browser_automation.py','browser_catalog.py','browser_topics.py','browser_cloud.py'):
                subprocess.run([sys.executable,str(ROOT/'tests'/name)],cwd=ROOT,env=env,check=True,timeout=180)
        finally:
            server.close()
            server.task_dispatcher.shutdown()
            thread.join(timeout=5)



if __name__=='__main__':main()
