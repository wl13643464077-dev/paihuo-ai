"""强制单进程:同一个数据库只允许一个应用进程(uvicorn worker)在跑。

引擎队列、任务锁、实时推送订阅都只存在于进程内存里。多开一个 worker
(或误启动第二份服务)时,两个进程会各自认领同一批任务、各发各的推送,
造成重复扣点和状态错乱。这里在启动时对数据目录下的专用锁文件拿
**非阻塞独占 flock**,拿不到就明确报错退出。

安全打开方式沿用 db.py 迁移锁的思路:``O_NOFOLLOW`` 拒绝符号链接,
打开后校验"普通文件、单链接、属主是自己",并确认打开的 inode 就是路径
上的那个;锁由内核在进程退出(含崩溃/SIGKILL)时自动释放,不会留下死锁。
"""
from __future__ import annotations

import fcntl
import os
import stat
import threading

LOCK_SUFFIX = ".instance.lock"


class InstanceLockError(RuntimeError):
    """已有另一个进程持有实例锁,或锁文件不安全。"""


_guard = threading.Lock()
# 锁文件路径 -> 持有的文件描述符。进程存活期间一直持有,不主动关闭。
_held: dict[str, int] = {}


def lock_path_for(db_path) -> str:
    """锁文件放在真实数据库文件旁边;符号链接别名解析到同一把锁。"""
    raw = os.fspath(db_path)
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise InstanceLockError("数据库路径必须是本地文件,无法加实例锁")
    return os.path.realpath(os.path.abspath(raw)) + LOCK_SUFFIX


def _verify(fd: int, path: str) -> None:
    opened = os.fstat(fd)
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise InstanceLockError(f"实例锁文件消失: {path}") from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise InstanceLockError(f"实例锁文件被替换: {path}")
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise InstanceLockError(f"实例锁必须是单链接普通文件: {path}")
    if opened.st_uid != os.geteuid():
        raise InstanceLockError(f"实例锁文件属主不对: {path}")
    if stat.S_IMODE(opened.st_mode) & 0o077:
        # 属主是自己且是单链接普通文件,收紧权限即可,不必拒绝启动。
        os.fchmod(fd, 0o600)


def _holder_pid(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return "?"
    try:
        text = os.read(fd, 32).decode("ascii", "replace").strip()
    except OSError:
        return "?"
    finally:
        os.close(fd)
    return text if text.isdigit() else "?"


def acquire(db_path) -> str:
    """拿实例锁;同一进程对同一把锁重复调用是幂等的(测试会多次触发启动)。

    返回锁文件路径。拿不到(另一进程持有)或锁文件不安全时抛 InstanceLockError。
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise InstanceLockError("系统不支持 O_NOFOLLOW,无法安全加实例锁")
    path = lock_path_for(db_path)
    with _guard:
        if path in _held:
            return path
        flags = (os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise InstanceLockError(f"无法安全打开实例锁文件: {path}") from exc
        try:
            _verify(fd, path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstanceLockError(
                    f"已有另一个派活进程(pid={_holder_pid(path)})在使用同一数据库,"
                    f"本进程拒绝启动。只能运行 1 个 worker。锁文件: {path}"
                ) from exc
            except OSError as exc:
                raise InstanceLockError(f"实例锁加锁失败: {path}") from exc
            # 加锁后再核一次:等待期间路径若被换掉,锁住旧 inode 不算数。
            _verify(fd, path)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except BaseException:
            os.close(fd)
            raise
        _held[path] = fd
        return path


def held_paths() -> list[str]:
    with _guard:
        return sorted(_held)


def release_all_for_tests() -> None:
    """仅测试使用:释放本进程持有的所有实例锁。"""
    with _guard:
        for fd in _held.values():
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        _held.clear()
