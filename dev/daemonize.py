#!/usr/bin/env python3
"""Run a command in its own session, detached from this terminal.

    uv run python dev/daemonize.py /tmp/out.log bash dev/laguna_quant_ab.sh

WHY THIS EXISTS. Long background jobs launched from a Claude Code session die
when that session exits, and `nohup` alone does not save them — that stranded a
PAUSED mission and a DOWN server once already. The usual fix is `setsid`, which
several of our scripts' header comments recommend, but **macOS has no setsid**;
the recommendation was never runnable here. This is the portable equivalent.

Double-fork so the final process is not a session leader and can never acquire a
controlling terminal: fork, setsid, fork again, then exec. stdin comes from
/dev/null and stdout/stderr both append to the named log.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: daemonize.py <logfile> <command> [args...]")
    logfile, argv = sys.argv[1], sys.argv[2:]

    # Resolve before forking so a bad path fails loudly in the foreground
    # rather than silently in a detached child nobody is watching.
    logfile = os.path.abspath(logfile)
    os.makedirs(os.path.dirname(logfile), exist_ok=True)

    if os.fork() > 0:
        os._exit(0)  # parent returns to the shell immediately
    os.setsid()  # new session; no controlling terminal
    if os.fork() > 0:
        os._exit(0)  # not a session leader, so it cannot reacquire one

    fd_null = os.open(os.devnull, os.O_RDONLY)
    fd_log = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd_null, 0)
    os.dup2(fd_log, 1)
    os.dup2(fd_log, 2)
    for fd in (fd_null, fd_log):
        if fd > 2:
            os.close(fd)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
