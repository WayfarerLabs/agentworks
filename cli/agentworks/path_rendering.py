"""How a host path is spelled to an operator: the one repo-wide rule.

Every string a human reads that names a file on the machine they are
sitting at goes through :func:`format_host_path`, so the rendering is a
property of the operator surface as a whole rather than a decision each
message makes for itself. ``tests/test_operator_path_rendering.py`` is
the guard over that invariant.

**This module is a top-level LEAF on purpose, and it imports only
``pathlib``.** It sits below the output contract rather than inside it,
because most of what needs the rule never touches an output handler:
``config/load.py`` prints straight to ``sys.stderr`` before any command
runs, ``schema/errors.py`` builds the text of an exception, and the
config and completion commands write through ``typer.echo``. Being a
leaf is also what lets ``agentworks.schema`` reach it: that package must
stay importable on its own (see ``schema/__init__.py``), so it may only
import top-level leaves. This is the same constraint ``source_location``
and ``declared_resource`` already sit at top level for.
"""

from __future__ import annotations

from pathlib import Path


def format_host_path(file: Path) -> str:
    """Render a file path operator-friendly: ``~`` plus the host path
    separator when under ``$HOME``, else the bare absolute path. Relative
    paths render as-is.

    **This is the one way a host path is spelled to an operator.** Every
    string a human reads that names a file on the machine they are
    sitting at goes through here: doctor rows, command confirmations,
    error messages, and hints alike. The rule is uniform on purpose. A
    screen that shows ``~/.config/...`` on one line and
    ``/home/you/.config/...`` on the next teaches a reader that the
    difference carries meaning, and it does not; that mixed screen is the
    defect this helper exists to prevent, and it has recurred twice.

    Four kinds of path are deliberately NOT rendered here, and each
    exclusion is load-bearing rather than an oversight:

    - **Paths on a VM or other remote.** ``$HOME`` here is the *host's*
      home, so abbreviating a remote path against it is not merely
      useless but actively wrong: an operator whose own username matches
      the VM's would see ``/home/dev/workspaces/w`` collapse to
      ``~/workspaces/w`` and go looking on the wrong machine. Remote
      paths stay absolute. See ``workspaces/manager/rehome.py`` for the
      density of them.
    - **Text written into a file another program parses**, where ``~`` is
      not expanded in that position: the PowerShell ``$PROFILE``
      source line (``completions/install.py``), and the OpenSSH config
      writer, which has its own ``_to_ssh_path`` because it is generating
      config for ``ssh`` to read rather than prose for a human.
    - **Verbatim echoes of what the operator typed**, such as
      ``admin.dotfiles_source`` (which may be a git URL, not a path at
      all) and ``apt.source_file``. Quoting input back unchanged is the
      point of those messages.
    - **The body of an SSH log**, which is a verbatim transcript whose
      command lines are the literal argv that ran. Naming the log file is
      this rule's job (``ssh.py``'s ``display_path``); rewriting the
      prose lines inside it while the ``$ ssh -i /home/you/...`` lines
      beside them stayed absolute would recreate the mixed rendering one
      level down.

    A path outside ``$HOME`` needs no exclusion: it already falls through
    to the absolute form.
    """
    if file.is_absolute():
        try:
            return str(Path("~") / file.relative_to(Path.home()))
        except ValueError:
            return str(file)
    return str(file)
