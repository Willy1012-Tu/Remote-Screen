"""Cloudflare Quick Tunnel (cloudflared) discovery, download, and lifecycle management."""

from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence
import urllib.request


CLOUDFLARED_VERSION = "2026.11.1"
CLOUDFLARED_DOWNLOAD_URL = (
    f"https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}/cloudflared-windows-amd64.exe"
)
CLOUDFLARED_SHA256 = "341deda3db5ec498a0482254d6c38c0b7cf01a30c37bb5dd95d2216d08955e35"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _assign_kill_on_close_job(pid: int) -> int | None:
    """Attach child pid to a Windows Job Object with KILL_ON_JOB_CLOSE."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    job_handle = None
    proc_handle = None
    try:
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            return None

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job_handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job_handle)
            return None

        proc_handle = kernel32.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc_handle:
            kernel32.CloseHandle(job_handle)
            return None

        assigned = kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        kernel32.CloseHandle(proc_handle)
        proc_handle = None
        if not assigned:
            kernel32.CloseHandle(job_handle)
            return None
        return int(job_handle)
    except Exception:
        try:
            if proc_handle:
                kernel32.CloseHandle(proc_handle)
            if job_handle:
                kernel32.CloseHandle(job_handle)
        except Exception:
            pass
        return None


def _close_job_handle(job_handle: int | None) -> None:
    if sys.platform != "win32" or not job_handle:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except Exception:
        pass


def default_cache_path() -> Path:
    """Return per-user cache path for cloudflared.exe."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home()
    return root / "RemoteScreen" / "bin" / "cloudflared.exe"


def find_cloudflared(app_dir: Path | None = None) -> Path | None:
    """Locate an existing cloudflared executable."""
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)
    if app_dir is not None:
        local_bin = app_dir / "bin" / "cloudflared.exe"
        if local_bin.is_file():
            return local_bin
    cached = default_cache_path()
    if cached.is_file():
        return cached
    return None


def build_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext combining Windows roots with certifi's CA bundle."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def verify_file_sha256(path: Path, expected_sha256: str) -> bool:
    """Return True if path's SHA-256 hex digest matches expected_sha256."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return hmac.compare_digest(digest.hexdigest().lower(), expected_sha256.strip().lower())


def _download_file(url: str, dest: Path, expected_sha256: str | None = CLOUDFLARED_SHA256) -> None:
    tmp = dest.with_name(f"{dest.name}.tmp")
    req = urllib.request.Request(url, headers={"User-Agent": "RemoteScreen/1.0"})
    ssl_ctx = build_ssl_context()
    digest = hashlib.sha256() if expected_sha256 else None
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_ctx) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if digest is not None:
                    digest.update(chunk)
                fh.write(chunk)
        if expected_sha256 and digest is not None:
            actual = digest.hexdigest().lower()
            if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
                raise RuntimeError(
                    f"SHA-256 mismatch for downloaded cloudflared: expected {expected_sha256}, got {actual}"
                )
        os.replace(tmp, dest)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def ensure_cloudflared(
    app_dir: Path | None = None,
    target_path: Path | None = None,
    download_fn: Callable[[str, Path], None] | None = None,
    expected_sha256: str | None = CLOUDFLARED_SHA256,
) -> Path:
    """Return path to cloudflared.exe, downloading and verifying SHA-256 if not found."""
    if target_path is not None:
        if target_path.is_file():
            return target_path
        dest = target_path
    else:
        existing = find_cloudflared(app_dir=app_dir)
        if existing is not None:
            return existing
        dest = default_cache_path()

    dest.parent.mkdir(parents=True, exist_ok=True)
    logging.getLogger(__name__).info("Downloading cloudflared %s to %s ...", CLOUDFLARED_VERSION, dest)
    if download_fn is None:
        _download_file(CLOUDFLARED_DOWNLOAD_URL, dest, expected_sha256=expected_sha256)
    else:
        download_fn(CLOUDFLARED_DOWNLOAD_URL, dest)
        if expected_sha256 and dest.is_file():
            if not verify_file_sha256(dest, expected_sha256):
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise RuntimeError("SHA-256 mismatch for downloaded cloudflared")
    if not dest.is_file():
        raise RuntimeError(f"Download completed but {dest} is missing")
    return dest


def extract_trycloudflare_url(line: str) -> str | None:
    """Extract a trycloudflare.com HTTPS URL from a log line."""
    match = TRYCLOUDFLARE_RE.search(line)
    return match.group(0) if match else None


class CloudflareTunnel:
    """Manages a cloudflared Quick Tunnel subprocess."""

    def __init__(
        self,
        binary: Path,
        command_args: Sequence[str] | None = None,
    ) -> None:
        self.binary = Path(binary)
        self._command_args = list(command_args) if command_args is not None else None
        self._proc: subprocess.Popen[str] | None = None
        self._job_handle: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._recent_logs: deque[str] = deque(maxlen=50)
        self._lock = threading.Lock()
        self._public_url: str | None = None

    @property
    def public_url(self) -> str | None:
        return self._public_url

    @property
    def recent_logs(self) -> list[str]:
        return list(self._recent_logs)

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self, local_url: str, timeout: float = 30.0) -> str:
        """Start cloudflared and wait until the public trycloudflare.com URL appears."""
        if self._proc is not None:
            raise RuntimeError("Tunnel already started")

        cmd = (
            [str(self.binary), *self._command_args]
            if self._command_args is not None
            else [str(self.binary), "tunnel", "--url", local_url, "--no-autoupdate"]
        )

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._proc = proc
        self._job_handle = _assign_kill_on_close_job(proc.pid)

        line_queue: queue.Queue[str | None] = queue.Queue()
        url_found = threading.Event()

        def _drain_output() -> None:
            stream = proc.stdout
            if stream is None:
                line_queue.put(None)
                return
            try:
                for raw_line in stream:
                    clean = raw_line.rstrip("\r\n")
                    self._recent_logs.append(clean)
                    if not url_found.is_set():
                        line_queue.put(clean)
            except Exception:
                pass
            finally:
                if not url_found.is_set():
                    line_queue.put(None)

        thread = threading.Thread(target=_drain_output, name="cloudflared-stdout", daemon=True)
        self._reader_thread = thread
        thread.start()

        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for trycloudflare.com URL")
                try:
                    line = line_queue.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    if proc.poll() is not None and line_queue.empty():
                        raise RuntimeError("cloudflared exited before providing a tunnel URL")
                    continue

                if line is None:
                    raise RuntimeError("cloudflared exited before providing a tunnel URL")

                url = extract_trycloudflare_url(line)
                if url:
                    url_found.set()
                    self._public_url = url
                    return url
        except Exception as exc:
            logs = "\n".join(self._recent_logs)
            self.stop()
            detail = f"{exc}" + (f"\nRecent logs:\n{logs}" if logs else "")
            raise RuntimeError(detail) from exc

    def stop(self) -> None:
        """Terminate cloudflared and clean up handles idempotently."""
        with self._lock:
            proc = self._proc
            self._proc = None
            job = self._job_handle
            self._job_handle = None
            thread = self._reader_thread
            self._reader_thread = None

        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
            except Exception:
                pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        _close_job_handle(job)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
