"""Command parsing utilities.

This module provides a lightweight parser for textual commands used by the
application.  A command consists of a name followed by zero or more space‑
separated arguments.  Arguments may be quoted to include whitespace.

Typical usage::

    >>> from parser import parse_command
    >>> cmd = parse_command('create_user "Alice Smith" alice@example.com')
    >>> cmd.name
    'create_user'
    >>> cmd.args
    ['Alice Smith', 'alice@example.com']

The implementation relies on :mod:`shlex` to perform POSIX‑compatible tokenisation.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True, slots=True)
class Command:
    """Represent a parsed command.

    Attributes
    ----------
    name: str
        The command identifier (the first token of the input string).
    args: List[str]
        Positional arguments following the command name.
    """
    name: str
    args: List[str]


def parse_command(command_line: str) -> Command:
    """Parse a raw command line into a :class:`Command` instance.

    Parameters
    ----------
    command_line: str
        The raw input string entered by the user or read from a script.

    Returns
    -------
    Command
        An immutable object containing the command name and its arguments.

    Raises
    ------
    ValueError
        If ``command_line`` is empty or contains only whitespace.
    """
    if not command_line or command_line.strip() == "":
        raise ValueError("Command line must contain at least a command name.")

    # shlex.split respects quoted substrings and handles escaped characters.
    tokens = shlex.split(command_line)

    # The first token is the command name; the rest are arguments.
    name, *args = tokens
    return Command(name=name, args=args)


__all__: List[str] = ["Command", "parse_command"]
