"""Private operator files. Empty files are secured before any secret is written."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat


def _windows_descriptor(path: Path, *, secure: bool) -> None:
    from ctypes import wintypes as w
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ptr), ptr]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ptr, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ptr]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ptr]
    adv.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ptr]
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise PermissionError("Cannot inspect service file owner")
    sid_string = w.LPWSTR()
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise PermissionError("Cannot inspect service file owner")
        sid = ctypes.cast(buffer, ctypes.POINTER(ptr))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise PermissionError("Cannot inspect service file owner")
        expected = f"D:P(A;;FA;;;{sid_string.value})(A;;FA;;;SY)"
    finally:
        if sid_string:
            kernel.LocalFree(ctypes.cast(sid_string, ptr))
        kernel.CloseHandle(token)
    descriptor = ptr()
    if secure:
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(expected, 1, ctypes.byref(descriptor), None):
            raise PermissionError("Cannot build private service file permissions")
        try:
            if not adv.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
                raise PermissionError("Cannot protect service file")
        finally:
            kernel.LocalFree(descriptor)
    size = w.DWORD()
    adv.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not adv.GetFileSecurityW(str(path), 4, buffer, size, ctypes.byref(size)):
        raise PermissionError("Cannot verify service file permissions")
    actual = w.LPWSTR()
    if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, ctypes.byref(actual), None):
        raise PermissionError("Cannot verify service file permissions")
    try:
        if actual.value != expected:
            raise PermissionError("Service file permissions are not private")
    finally:
        kernel.LocalFree(ctypes.cast(actual, ptr))


def check_path(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("Service file must have an absolute path")
    for part in (path, *path.parents):
        if part.exists():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("Service file path contains a link or reparse point")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise PermissionError("Service file has multiple hard links")


def private_permissions(path: Path, *, secure: bool = False) -> None:
    check_path(path)
    if os.name == "nt":
        _windows_descriptor(path, secure=secure)
    else:
        if secure:
            path.chmod(0o700 if path.is_dir() else 0o600)
        info = path.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if path.is_dir() else 0o600):
            raise PermissionError("Service file permissions are not private")


def create_private(path: Path, content: bytes) -> None:
    check_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        private_permissions(path, secure=True)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(fd)
        private_permissions(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def read_private(path: Path, limit: int = 1024) -> bytes:
    private_permissions(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Service file exceeds size limit")
    private_permissions(path)
    return data
