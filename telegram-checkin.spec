from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root/'app.py')], pathex=[str(root)],
    datas=[(str(root/'gui.html'), '.'), (str(root/'VERSION'), '.'), (str(root/'static'), 'static')],
    hiddenimports=['playwright.__main__', 'tzdata'],
    excludes=['tests', 'tkinter'], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='telegram-checkin',
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='telegram-checkin')
