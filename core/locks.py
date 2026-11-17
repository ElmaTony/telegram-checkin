"""OS-held locks: automatically released on exit, shared by GUI and CLI."""
import os
from pathlib import Path


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError("账号或任务正在被其他进程使用，请等待结束") from error
        self.stream = stream
        return self

    def release(self):
        if self.stream:
            stream, self.stream = self.stream, None
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
