"""agw-launch: the Agentworks launcher, the guest half of every request.

The client sends this file's source on the connection's stdin behind a constant
first stage (``ssh_process.STAGE0``), which reads it with exact-length reads and
``exec``s it. The first stage's top-level names (``os``, ``sys``, ``S``, ``a``,
``s``, ``f``, ``r``, ``g``) are the globals this file runs in, so it defines
none of them at module level. It has no import-time effect: it defines
``main(f, g)`` and calls it only when run as the first stage's program, so the
client can import the file for the two definitions it shares without running
anything. Every other import sits in one ``try`` inside ``main``, because the
client imports this module on Windows, where ``pwd`` does not exist.

``f`` is the descriptor the request arrives on (stdin, or the handover socket)
and ``g`` is this file's own source, which a parked launcher serves again.

Plain syntax and the standard library only: the guest's python3 may be any
release a supported Debian ships.
"""

STDLIB_MISSING_STATUS = 69
"""Exit status when the standard library is incomplete (Debian's python3-minimal
has ``os``, ``sys`` and ``socket`` but not ``json``). Written before any marker."""


def refusal_status(code: str) -> int:
    """The shell-convention exit status for a refusal code.

    127 for a command that was not found, 126 for any other ``exec`` failure and
    125 for a working directory or handover refusal. The guest uses it in
    ``tty`` mode, where there is no trailer, and the client uses the same table
    for ``stream``, so each side reports the same status.
    """
    if code == "exec ENOENT":
        return 127
    if code.startswith("exec "):
        return 126
    return 125


def main(f: int, g: bytes) -> None:
    import os
    import sys

    try:
        import contextlib
        import errno
        import json
        import pwd
        import select
        import signal
        import socket
        import stat
        import subprocess
        import time
    except ImportError:
        sys.exit(STDLIB_MISSING_STATUS)

    fixed_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/games:/usr/games"
    handover_wait_seconds = 30

    # -- 1. Read: the envelope and the passwd entry. Every failure before the
    # start marker is an uncaught exception (exit 1) or the status above.

    def read_exact(count: int) -> bytes:
        parts = []
        while count:
            chunk = os.read(f, count)
            if not chunk:
                raise EOFError("short read")
            parts.append(chunk)
            count -= len(chunk)
        return b"".join(parts)

    def frame(data: bytes) -> bytes:
        return b"%010d" % len(data) + data

    def enc(text: str) -> bytes:
        return text.encode("utf-8", "surrogateescape")

    def emit(fd: int, data: bytes) -> None:
        # Descriptors 1 and 2 share their open file descriptions with whatever ran
        # before this write (a startup file, the payload), which may have made
        # them non-blocking; a marker must not be lost to EAGAIN. The peer may
        # already be gone; there is nothing useful to do about that.
        try:
            os.set_blocking(fd, True)
            while data:
                data = data[os.write(fd, data) :]
        except OSError:
            pass

    request = json.loads(read_exact(int(read_exact(10))))
    mode = request["mode"]
    marker = b"\x1e" + request["nonce"].encode("ascii") + b" "
    uid = os.getuid()
    entry = pwd.getpwuid(uid)
    home = entry.pw_dir
    shell = entry.pw_shell or "/bin/sh"
    workdir = request["cwd"] or home

    def refuse(code: str) -> None:
        emit(1, marker + b"end\n")
        emit(2, marker + b"refused " + code.encode("ascii") + b"\n")
        sys.exit(0)

    def exec_code(error: OSError) -> str:
        return "exec " + errno.errorcode.get(error.errno or 0, "UNKNOWN")

    def build_environment() -> dict[bytes, bytes]:
        env = {
            "HOME": home,
            "USER": entry.pw_name,
            "LOGNAME": entry.pw_name,
            "SHELL": shell,
            "PATH": fixed_path,
            "LANG": "C.UTF-8",
            "SHLVL": "1",
        }
        if mode == "tty" and os.isatty(0):
            env["TERM"] = request["term"]
            with contextlib.suppress(OSError):
                env["SSH_TTY"] = os.ttyname(0)
        runtime_dir = f"/run/user/{uid}"
        try:
            info = os.stat(runtime_dir)
            if stat.S_ISDIR(info.st_mode) and info.st_uid == uid:
                env["XDG_RUNTIME_DIR"] = runtime_dir
        except OSError:
            pass
        env.update(request["env"])
        return {enc(name): enc(value) for name, value in env.items()}

    # Encoded before the start marker, so a string that cannot be encoded
    # fails before it.
    environment = build_environment()
    arguments = [enc(item) for item in request["argv"]] if request["kind"] == "argv" else []
    source = enc(request["source"]) if request["kind"] == "script" else b""

    def reset_signals(include_python_ignores: bool) -> None:
        for number in signal.valid_signals():
            if number in (signal.SIGKILL, signal.SIGSTOP):
                continue
            if not include_python_ignores and number in (signal.SIGPIPE, signal.SIGXFSZ):
                continue
            try:
                if signal.getsignal(number) == signal.SIG_IGN:
                    signal.signal(number, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        signal.pthread_sigmask(signal.SIG_SETMASK, ())

    # -- tty: the terminal half of an attach. No check, no markers and no
    # child: it becomes the shell, so the shell is the session leader.

    if mode == "tty":

        def tty_refusal(code: str) -> None:
            emit(2, ("agw: refused: " + code + "\n").encode("ascii"))
            sys.exit(refusal_status(code))

        reset_signals(True)
        os.umask(0o022)
        try:
            os.chdir(workdir)
        except OSError:
            tty_refusal("cwd")
        for name in os.listdir("/proc/self/fd"):
            if int(name) > 2:
                with contextlib.suppress(OSError):
                    os.close(int(name))
        flags = ["-l", "-i"] if os.isatty(0) else ["-l"]
        try:
            os.execve(shell, [shell, *flags], environment)
        except OSError as error:
            tty_refusal(exec_code(error))

    # -- 2. Check the streams: descriptors 1 and 2 must each be a FIFO or a
    # socket, and distinct. A startup file that redirected one fails this.

    out_info = os.fstat(1)
    err_info = os.fstat(2)
    for info in (out_info, err_info):
        if not (stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            sys.exit("agw-launch: a standard stream is not a pipe or socket")
    if (out_info.st_dev, out_info.st_ino) == (err_info.st_dev, err_info.st_ino):
        sys.exit("agw-launch: standard output and error are the same stream")

    # The script is staged before the start marker, so a failure to create or
    # fill it is a launcher failure, not an outcome that may have run.
    script_fd = -1
    pass_fds: tuple[int, ...] = ()
    if request["kind"] == "script":
        try:
            # MFD_NOEXEC_SEAL (kernel 6.3 and later), retried without it only on
            # EINVAL; any other failure (EMFILE, say) is a launcher failure.
            script_fd = os.memfd_create("agw-script", os.MFD_CLOEXEC | 0x8)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            script_fd = os.memfd_create("agw-script", os.MFD_CLOEXEC)
        remaining = source
        while remaining:
            remaining = remaining[os.write(script_fd, remaining) :]
        interpreter = {"sh": "/bin/sh", "bash": "/bin/bash", "user_default": shell}[request["shell"]]
        arguments = [enc(interpreter)]
        if request["login"]:
            arguments.append(b"-l")
        arguments.append(b"/dev/fd/%d" % script_fd)
        pass_fds = (script_fd,)
    # -- 3. Start marker, to stdout and then stderr.

    emit(1, marker + b"start\n")
    emit(2, marker + b"start\n")

    # -- 4. Reset what the payload inherits.

    reset_signals(False)
    os.umask(0o022)
    try:
        os.chdir(workdir)
    except OSError:
        refuse("cwd")

    # -- park: wait for the terminal half to collect the request.

    if mode == "park":
        try:
            shell_info = os.stat(shell)
        except OSError as error:
            refuse(exec_code(error))
        else:
            if not stat.S_ISREG(shell_info.st_mode) or not os.access(shell, os.X_OK):
                refuse("exec EACCES")

        def stop(number: int, frame_: object) -> None:
            sys.exit(1)

        signal.signal(signal.SIGHUP, stop)
        signal.signal(signal.SIGTERM, stop)
        directory = "/tmp/agw-handover-" + request["handover"]
        server = socket.socket(socket.AF_UNIX)
        try:
            os.mkdir(directory, 0o700)
        except OSError:
            refuse("handover")
        try:
            previous = os.umask(0o077)
            try:
                server.bind(directory + "/s")
            finally:
                os.umask(previous)
            server.listen(1)
            emit(1, marker + b"parked\n")
            until = time.monotonic() + handover_wait_seconds
            while True:
                left = until - time.monotonic()
                if left <= 0:
                    refuse("handover")
                ready = select.select([server.fileno(), 0], [], [], left)[0]
                if server.fileno() in ready:
                    break
                if ready and not os.read(0, 4096):
                    refuse("handover")
            connection = server.accept()[0]
            terminal = dict(request, mode="tty")
            connection.sendall(frame(g))
            connection.sendall(frame(json.dumps(terminal, ensure_ascii=True, separators=(",", ":")).encode("ascii")))
            connection.close()
            emit(1, marker + b"end\n")
            emit(2, marker + b"exit 0\n")
            return
        finally:
            server.close()
            for path, remove in ((directory + "/s", os.unlink), (directory, os.rmdir)):
                with contextlib.suppress(OSError):
                    remove(path)

    # -- 5. Run the payload as a child, not a shell.

    try:
        child = subprocess.Popen(
            arguments,
            env=environment,
            close_fds=True,
            restore_signals=True,
            pass_fds=pass_fds,
        )
    except OSError as error:
        refuse(exec_code(error))
    code = child.wait()
    if script_fd >= 0:
        os.close(script_fd)

    # -- 6. Complete: the end marker on stdout, then the trailer on stderr
    # (emit puts the shared descriptors back to blocking first).

    emit(1, marker + b"end\n")
    emit(2, marker + b"exit %d\n" % (128 - code if code < 0 else code))


if __name__ == "__main__":
    main(globals()["f"], globals()["g"])
