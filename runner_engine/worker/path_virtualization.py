from __future__ import annotations

import builtins
import errno
import io
import os
import posixpath
import shutil
import stat as stat_module
from pathlib import Path
from typing import Iterator


class _VirtualDirEntry:
    def __init__(self, layer: "PathVirtualization", parent: str, name: str):
        self._layer = layer
        self.name = name
        self.path = posixpath.join(parent, name) if parent != "/" else f"/{name}"

    def __fspath__(self) -> str:
        return self.path

    def inode(self) -> int:
        return self.stat(follow_symlinks=False).st_ino

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        mode = self.stat(follow_symlinks=follow_symlinks).st_mode
        return stat_module.S_ISDIR(mode)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        mode = self.stat(follow_symlinks=follow_symlinks).st_mode
        return stat_module.S_ISREG(mode)

    def is_symlink(self) -> bool:
        mode = self.stat(follow_symlinks=False).st_mode
        return stat_module.S_ISLNK(mode)

    def stat(self, *, follow_symlinks: bool = True):
        return self._layer.stat(self.path, follow_symlinks=follow_symlinks)


class _VirtualScandir:
    def __init__(self, entries: list[_VirtualDirEntry]):
        self._entries = iter(entries)

    def __iter__(self) -> "_VirtualScandir":
        return self

    def __next__(self) -> _VirtualDirEntry:
        return next(self._entries)

    def close(self) -> None:
        return

    def __enter__(self) -> "_VirtualScandir":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class PathVirtualization:
    """
    Lightweight Python-level filesystem compatibility layer.

    Relative paths keep normal cwd semantics.

    Absolute write operations are redirected into:
        <invocation_root>/absolute/<original absolute path>

    Absolute reads prefer the invocation-local version when it exists, otherwise
    they fall back to the real container path.

    This is intentionally not an OS sandbox. Native code or subprocesses that
    bypass Python's filesystem APIs still see the real container filesystem and
    remain constrained by the unprivileged child UID/GID.
    """

    def __init__(self, invocation_root: str | Path):
        self.invocation_root = Path(invocation_root).resolve()
        self.absolute_root = self.invocation_root / "absolute"
        self.absolute_root.mkdir(parents=True, exist_ok=True)

        self._tombstones: set[str] = set()
        self._installed = False

        self._open = builtins.open
        self._io_open = io.open
        self._os_open = os.open
        self._mkdir = os.mkdir
        self._unlink = os.unlink
        self._remove = os.remove
        self._rmdir = os.rmdir
        self._rename = os.rename
        self._replace = os.replace
        self._stat = os.stat
        self._lstat = os.lstat
        self._access = os.access
        self._listdir = os.listdir
        self._scandir = os.scandir
        self._readlink = os.readlink
        self._symlink = os.symlink
        self._link = os.link
        self._chmod = os.chmod
        self._utime = os.utime
        self._truncate = os.truncate
        self._chdir = os.chdir
        self._getcwd = os.getcwd
        self._getcwdb = os.getcwdb

    def install(self) -> None:
        if self._installed:
            return

        builtins.open = self.open
        io.open = self.io_open

        os.open = self.os_open
        os.mkdir = self.mkdir
        os.unlink = self.unlink
        os.remove = self.remove
        os.rmdir = self.rmdir
        os.rename = self.rename
        os.replace = self.replace
        os.stat = self.stat
        os.lstat = self.lstat
        os.access = self.access
        os.listdir = self.listdir
        os.scandir = self.scandir
        os.readlink = self.readlink
        os.symlink = self.symlink
        os.link = self.link
        os.chmod = self.chmod
        os.utime = self.utime
        os.truncate = self.truncate
        os.chdir = self.chdir
        os.getcwd = self.getcwd
        os.getcwdb = self.getcwdb

        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return

        builtins.open = self._open
        io.open = self._io_open

        os.open = self._os_open
        os.mkdir = self._mkdir
        os.unlink = self._unlink
        os.remove = self._remove
        os.rmdir = self._rmdir
        os.rename = self._rename
        os.replace = self._replace
        os.stat = self._stat
        os.lstat = self._lstat
        os.access = self._access
        os.listdir = self._listdir
        os.scandir = self._scandir
        os.readlink = self._readlink
        os.symlink = self._symlink
        os.link = self._link
        os.chmod = self._chmod
        os.utime = self._utime
        os.truncate = self._truncate
        os.chdir = self._chdir
        os.getcwd = self._getcwd
        os.getcwdb = self._getcwdb

        self._installed = False

    def _to_text(self, path) -> tuple[str | None, bool]:
        if isinstance(path, int):
            return None, False

        raw = os.fspath(path)
        is_bytes = isinstance(raw, bytes)
        return os.fsdecode(raw), is_bytes

    def _logical_absolute(self, path) -> tuple[str | None, bool]:
        text, is_bytes = self._to_text(path)
        if text is None:
            return None, is_bytes

        if posixpath.isabs(text):
            normalized = posixpath.normpath(text)
            invocation_root = str(self.invocation_root)

            if normalized == invocation_root or normalized.startswith(invocation_root + "/"):
                return None, is_bytes

            return normalized, is_bytes

        real_cwd = self._getcwd()
        try:
            relative = Path(real_cwd).resolve().relative_to(self.absolute_root)
        except ValueError:
            return None, is_bytes

        logical_cwd = "/" + relative.as_posix()
        logical = posixpath.normpath(posixpath.join(logical_cwd, text))
        return logical, is_bytes

    def _same_type(self, value: str, is_bytes: bool):
        return os.fsencode(value) if is_bytes else value

    def _candidate(self, logical: str) -> str:
        relative = logical.lstrip("/")
        candidate = self.absolute_root / relative
        return str(candidate)

    def _is_tombstoned(self, logical: str) -> bool:
        current = logical
        while True:
            if current in self._tombstones:
                return True
            if current == "/":
                return False
            current = posixpath.dirname(current) or "/"

    def _clear_tombstone(self, logical: str) -> None:
        self._tombstones.discard(logical)

    def _candidate_exists(self, logical: str) -> bool:
        candidate = self._candidate(logical)
        try:
            self._lstat(candidate)
            return True
        except FileNotFoundError:
            return False

    def _read_path(self, path):
        logical, is_bytes = self._logical_absolute(path)
        if logical is None:
            return path

        if self._candidate_exists(logical) or self._is_tombstoned(logical):
            return self._same_type(self._candidate(logical), is_bytes)

        return path

    def _materialize_existing_parents(self, logical: str) -> None:
        parent = posixpath.dirname(logical) or "/"
        if parent == "/":
            return

        parts = [part for part in parent.split("/") if part]
        real_current = "/"
        virtual_current = self.absolute_root

        for part in parts:
            real_current = posixpath.join(real_current, part)
            virtual_current = virtual_current / part

            if virtual_current.exists():
                continue

            try:
                st = self._stat(real_current)
            except FileNotFoundError:
                break

            if not stat_module.S_ISDIR(st.st_mode):
                break

            self._mkdir(virtual_current, 0o700)

    def _assert_no_virtual_symlink_parent(self, logical: str) -> None:
        relative = [part for part in logical.lstrip("/").split("/") if part]
        current = self.absolute_root

        for part in relative[:-1]:
            current = current / part

            try:
                st = self._lstat(current)
            except FileNotFoundError:
                return

            if stat_module.S_ISLNK(st.st_mode):
                raise PermissionError(
                    errno.EPERM,
                    "virtual absolute path may not traverse a symlink parent",
                    logical,
                )

    def _write_path(self, path, *, copy_existing: bool = False, exclusive: bool = False):
        logical, is_bytes = self._logical_absolute(path)
        if logical is None:
            return path

        self._materialize_existing_parents(logical)
        self._assert_no_virtual_symlink_parent(logical)

        candidate = self._candidate(logical)
        candidate_exists = self._candidate_exists(logical)
        real_exists = False

        try:
            self._lstat(logical)
            real_exists = not self._is_tombstoned(logical)
        except FileNotFoundError:
            pass

        if exclusive and (candidate_exists or real_exists):
            raise FileExistsError(errno.EEXIST, "file exists", logical)

        if copy_existing and not candidate_exists and real_exists:
            st = self._lstat(logical)

            if stat_module.S_ISREG(st.st_mode):
                shutil.copy2(logical, candidate, follow_symlinks=False)
            elif stat_module.S_ISDIR(st.st_mode):
                shutil.copytree(logical, candidate, symlinks=True)
            elif stat_module.S_ISLNK(st.st_mode):
                self._symlink(self._readlink(logical), candidate)

        self._clear_tombstone(logical)
        return self._same_type(candidate, is_bytes)

    def _delete_logical(self, path, *, directory: bool) -> None:
        logical, _ = self._logical_absolute(path)
        if logical is None:
            if directory:
                self._rmdir(path)
            else:
                self._unlink(path)
            return

        candidate = self._candidate(logical)

        try:
            st = self._lstat(candidate)
        except FileNotFoundError:
            st = None

        if st is not None:
            if directory:
                self._rmdir(candidate)
            else:
                self._unlink(candidate)

        real_exists = False
        try:
            real_st = self._lstat(logical)
            real_exists = True
        except FileNotFoundError:
            real_st = None

        if st is None and not real_exists:
            raise FileNotFoundError(errno.ENOENT, "no such file or directory", logical)

        if directory and real_st is not None:
            visible = self.listdir(logical)
            if visible:
                raise OSError(errno.ENOTEMPTY, "directory not empty", logical)

        self._tombstones.add(logical)

    def open(self, file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))

        if not writing:
            file = self._read_path(file)
        else:
            copy_existing = ("a" in mode) or ("+" in mode and "w" not in mode and "x" not in mode)
            file = self._write_path(
                file,
                copy_existing=copy_existing,
                exclusive="x" in mode,
            )

        return self._open(
            file,
            mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )

    def io_open(self, file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))

        if not writing:
            file = self._read_path(file)
        else:
            copy_existing = ("a" in mode) or ("+" in mode and "w" not in mode and "x" not in mode)
            file = self._write_path(
                file,
                copy_existing=copy_existing,
                exclusive="x" in mode,
            )

        return self._io_open(
            file,
            mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )

    def os_open(self, path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            return self._os_open(path, flags, mode, dir_fd=dir_fd)

        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        writing = bool(flags & write_flags)

        if not writing:
            path = self._read_path(path)
        else:
            exclusive = bool(flags & os.O_EXCL and flags & os.O_CREAT)
            copy_existing = not bool(flags & os.O_TRUNC) and not exclusive
            path = self._write_path(
                path,
                copy_existing=copy_existing,
                exclusive=exclusive,
            )

        return self._os_open(path, flags, mode)

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            return self._mkdir(path, mode, dir_fd=dir_fd)

        target = self._write_path(path, exclusive=True)
        return self._mkdir(target, mode)

    def unlink(self, path, *, dir_fd=None):
        if dir_fd is not None:
            return self._unlink(path, dir_fd=dir_fd)
        return self._delete_logical(path, directory=False)

    def remove(self, path, *, dir_fd=None):
        if dir_fd is not None:
            return self._remove(path, dir_fd=dir_fd)
        return self._delete_logical(path, directory=False)

    def rmdir(self, path, *, dir_fd=None):
        if dir_fd is not None:
            return self._rmdir(path, dir_fd=dir_fd)
        return self._delete_logical(path, directory=True)

    def _copy_up_source(self, path):
        logical, is_bytes = self._logical_absolute(path)
        if logical is None:
            return path, None

        candidate = self._candidate(logical)
        if self._candidate_exists(logical):
            return self._same_type(candidate, is_bytes), logical

        if self._is_tombstoned(logical):
            raise FileNotFoundError(errno.ENOENT, "no such file or directory", logical)

        try:
            st = self._lstat(logical)
        except FileNotFoundError:
            raise FileNotFoundError(errno.ENOENT, "no such file or directory", logical)

        self._materialize_existing_parents(logical)

        if stat_module.S_ISREG(st.st_mode):
            shutil.copy2(logical, candidate, follow_symlinks=False)
        elif stat_module.S_ISDIR(st.st_mode):
            shutil.copytree(logical, candidate, symlinks=True)
        elif stat_module.S_ISLNK(st.st_mode):
            self._symlink(self._readlink(logical), candidate)
        else:
            raise OSError(errno.ENOTSUP, "unsupported virtual rename source", logical)

        return self._same_type(candidate, is_bytes), logical

    def rename(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is not None or dst_dir_fd is not None:
            return self._rename(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        source, source_logical = self._copy_up_source(src)
        target = self._write_path(dst, exclusive=True)
        result = self._rename(source, target)

        if source_logical is not None:
            self._tombstones.add(source_logical)

        return result

    def replace(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is not None or dst_dir_fd is not None:
            return self._replace(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        source, source_logical = self._copy_up_source(src)
        target = self._write_path(dst, copy_existing=False)
        result = self._replace(source, target)

        if source_logical is not None:
            self._tombstones.add(source_logical)

        return result

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            return self._stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        target = self._read_path(path)
        return self._stat(target, follow_symlinks=follow_symlinks)

    def lstat(self, path, *, dir_fd=None):
        if dir_fd is not None:
            return self._lstat(path, dir_fd=dir_fd)

        target = self._read_path(path)
        return self._lstat(target)

    def access(self, path, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
        if dir_fd is not None:
            return self._access(
                path,
                mode,
                dir_fd=dir_fd,
                effective_ids=effective_ids,
                follow_symlinks=follow_symlinks,
            )

        target = self._read_path(path)
        return self._access(
            target,
            mode,
            effective_ids=effective_ids,
            follow_symlinks=follow_symlinks,
        )

    def listdir(self, path="."):
        logical, is_bytes = self._logical_absolute(path)
        if logical is None:
            return self._listdir(path)

        if self._is_tombstoned(logical) and not self._candidate_exists(logical):
            raise FileNotFoundError(errno.ENOENT, "no such file or directory", logical)

        names = set()

        try:
            real_names = self._listdir(logical)
        except FileNotFoundError:
            real_names = []

        for name in real_names:
            text = os.fsdecode(name)
            child = posixpath.join(logical, text)
            if not self._is_tombstoned(child):
                names.add(text)

        candidate = self._candidate(logical)
        try:
            virtual_names = self._listdir(candidate)
        except FileNotFoundError:
            virtual_names = []

        for name in virtual_names:
            names.add(os.fsdecode(name))

        result = sorted(names)
        if is_bytes:
            return [os.fsencode(name) for name in result]
        return result

    def scandir(self, path=".") -> Iterator[_VirtualDirEntry]:
        logical, _ = self._logical_absolute(path)
        if logical is None:
            return self._scandir(path)

        entries = [
            _VirtualDirEntry(self, logical, os.fsdecode(name))
            for name in self.listdir(logical)
        ]
        return _VirtualScandir(entries)

    def readlink(self, path, *, dir_fd=None):
        if dir_fd is not None:
            return self._readlink(path, dir_fd=dir_fd)

        target = self._read_path(path)
        return self._readlink(target)

    def symlink(self, src, dst, target_is_directory=False, *, dir_fd=None):
        if dir_fd is not None:
            return self._symlink(
                src,
                dst,
                target_is_directory=target_is_directory,
                dir_fd=dir_fd,
            )

        logical, _ = self._logical_absolute(dst)
        if logical is None:
            return self._symlink(src, dst, target_is_directory=target_is_directory)

        target = self._write_path(dst, exclusive=True)

        # Absolute symlink targets could escape the invocation-local tree on a
        # later write. Keep them lexical/relative inside the virtual namespace.
        src_text = os.fsdecode(os.fspath(src))
        if posixpath.isabs(src_text):
            raise PermissionError(
                errno.EPERM,
                "absolute symlink targets are not allowed in virtual absolute paths",
                src_text,
            )

        return self._symlink(
            src,
            target,
            target_is_directory=target_is_directory,
        )

    def link(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        if src_dir_fd is not None or dst_dir_fd is not None:
            return self._link(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        source = self._read_path(src)
        target = self._write_path(dst, exclusive=True)
        return self._link(source, target, follow_symlinks=follow_symlinks)

    def chmod(self, path, mode, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            return self._chmod(
                path,
                mode,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        target = self._write_path(path, copy_existing=True)
        return self._chmod(target, mode, follow_symlinks=follow_symlinks)

    def utime(self, path, times=None, *, ns=None, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            return self._utime(
                path,
                times,
                ns=ns,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        target = self._write_path(path, copy_existing=True)
        return self._utime(
            target,
            times,
            ns=ns,
            follow_symlinks=follow_symlinks,
        )

    def truncate(self, path, length):
        target = self._write_path(path, copy_existing=True)
        return self._truncate(target, length)

    def chdir(self, path):
        logical, _ = self._logical_absolute(path)
        if logical is None:
            return self._chdir(path)

        candidate = self._candidate(logical)

        if not self._candidate_exists(logical):
            if self._is_tombstoned(logical):
                raise FileNotFoundError(errno.ENOENT, "no such file or directory", logical)

            st = self._stat(logical)
            if not stat_module.S_ISDIR(st.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "not a directory", logical)

            self._materialize_existing_parents(posixpath.join(logical, ".mpr"))
            if not Path(candidate).exists():
                self._mkdir(candidate, 0o700)

        return self._chdir(candidate)

    def getcwd(self) -> str:
        current = Path(self._getcwd()).resolve()

        try:
            relative = current.relative_to(self.absolute_root)
        except ValueError:
            return self._getcwd()

        return "/" + relative.as_posix()

    def getcwdb(self) -> bytes:
        return os.fsencode(self.getcwd())


_ACTIVE_LAYER: PathVirtualization | None = None


def install_from_env() -> PathVirtualization | None:
    global _ACTIVE_LAYER

    invocation_root = os.environ.get("RUNNER_INVOCATION_ROOT")
    if not invocation_root:
        return None

    layer = PathVirtualization(invocation_root)
    layer.install()

    # Keep the mapping private to the trusted compatibility layer. User code does
    # not need the host-side scratch path.
    os.environ.pop("RUNNER_INVOCATION_ROOT", None)

    _ACTIVE_LAYER = layer
    return layer
