"""Registered applications: the only things Lumi may start.

A model, the renderer and the user's typed text can name an application only by its `appId`. What that
id means is decided here, from trusted configuration: a canonical absolute executable path, optional
fixed arguments, and a label. There is no way to supply a path, an argument, a working directory,
an environment variable, a URI or a shell command, and this module refuses a descriptor that tries.

Descriptors come from two places only, both under the user's control and neither reachable from the
renderer or a model: a short built-in list, and `LUMI_DESKTOP_REGISTERED_APPS` (a JSON document set by
Electron main from the user's own configuration). A Windows Start-menu search is not a source: an
installed program is not agent authority because it exists.
"""

import json
import ntpath
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import APP_ID_PATTERN, MAX_APPLICATION_LABEL, clean_text

MAX_REGISTERED_APPS: Final = 16
MAX_FIXED_ARGS: Final = 4
MAX_ARG_LENGTH: Final = 200

#: Executables that are a shell, an interpreter for arbitrary text, or a proxy that runs something else.
#: A registered descriptor can never name one, whatever directory it is in.
FORBIDDEN_EXECUTABLES: Final = frozenset(
    {
        "cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "wscript.exe", "cscript.exe",
        "mshta.exe", "rundll32.exe", "regsvr32.exe", "wmic.exe", "msiexec.exe", "bash.exe", "wsl.exe",
        "conhost.exe", "explorer.exe", "start.exe", "forfiles.exe", "schtasks.exe", "sc.exe", "reg.exe",
        "certutil.exe", "bitsadmin.exe", "curl.exe", "installutil.exe", "regasm.exe", "msbuild.exe",
        "runas.exe", "at.exe", "control.exe", "python.exe", "pythonw.exe", "py.exe", "node.exe", "npm.exe", "npx.exe",
        "java.exe", "javaw.exe", "mmc.exe", "cmstp.exe", "ftp.exe", "wt.exe", "regedit.exe", "msdt.exe", "pcalua.exe",
        "ssh.exe", "scp.exe", "sftp.exe", "telnet.exe", "wget.exe", "tar.exe", "bcdedit.exe", "diskpart.exe",
        "net.exe", "net1.exe", "netsh.exe", "taskkill.exe", "tasklist.exe", "takeown.exe", "icacls.exe", "attrib.exe",
        "vssadmin.exe", "sc.exe", "gpupdate.exe", "dism.exe", "esentutl.exe", "expand.exe", "extrac32.exe",
        "hh.exe", "ie4uinit.exe", "infdefaultinstall.exe", "mavinject.exe", "odbcconf.exe", "presentationhost.exe",
        "syncappvpublishingserver.exe", "xwizard.exe", "winrs.exe", "wsmprovhost.exe", "cscript.exe",
        "consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe", "winlogon.exe",
    }
)
#: Subdirectories of the trusted install roots that an ordinary user can write to. A program there is not
#: protected by the root being administrator-only.
USER_WRITABLE_PARTS: Final = frozenset({"temp", "tasks", "tracing", "debug", "registration", "spool", "tmp", "servicing"})


@dataclass(frozen=True, slots=True)
class RegisteredApp:
    app_id: str
    label: str
    #: Canonical absolute path. Worker-internal: never on the wire, in a ledger row or a card.
    executable: str
    args: tuple[str, ...] = ()

    @property
    def image(self) -> str:
        return ntpath.basename(self.executable).lower()


def canonical(path: str) -> str:
    """One spelling per file, for comparison. Case-insensitive, forward slashes folded."""
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\")))


def same_file(left: str, right: str) -> bool:
    """Do two paths name one file? Junctions, symlinks and case are resolved, so a registered path that
    goes through a link is still recognised as the process that is running from it."""
    if canonical(left) == canonical(right):
        return True
    return canonical(os.path.realpath(left)) == canonical(os.path.realpath(right))


def _trusted_roots() -> tuple[str, ...]:
    """Directories that only an administrator can write to."""
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    return tuple(canonical(value) for name in names if (value := os.environ.get(name)))


def _under(path: str, roots: Iterable[str]) -> bool:
    target = canonical(path)
    return any(target.startswith(root.rstrip("\\") + "\\") for root in roots)


def validate(entry: Mapping[str, object], *, roots: Iterable[str]) -> RegisteredApp:
    app_id = entry.get("appId")
    label = entry.get("label")
    executable = entry.get("executable")
    args = entry.get("args", [])
    if not isinstance(app_id, str) or APP_ID_PATTERN.fullmatch(app_id) is None:
        raise ValueError("appId is not valid")
    if not isinstance(label, str) or not 1 <= len(label) <= MAX_APPLICATION_LABEL or clean_text(label) != label:
        raise ValueError("label is not valid")
    if not isinstance(executable, str) or "\x00" in executable or "\n" in executable:
        raise ValueError("executable is not valid")
    if not ntpath.isabs(executable) or executable.startswith("\\\\") or ".." in executable.split("\\"):
        raise ValueError("executable must be a local absolute path")
    path = canonical(executable)
    if not path.endswith(".exe") or ntpath.basename(path) in FORBIDDEN_EXECUTABLES:
        raise ValueError("executable is not an allowed program")
    if not _under(path, roots):
        raise ValueError("executable is outside the trusted install roots")
    if USER_WRITABLE_PARTS & set(path.split("\\")[1:-1]):
        raise ValueError("executable is in a directory an ordinary user can write to")
    if not isinstance(args, list) or len(args) > MAX_FIXED_ARGS:
        raise ValueError("args are not valid")
    for arg in args:
        if not isinstance(arg, str) or len(arg) > MAX_ARG_LENGTH or "\x00" in arg or "\n" in arg:
            raise ValueError("args are not valid")
    return RegisteredApp(app_id=app_id, label=label, executable=executable, args=tuple(args))


class AppRegistry:
    """A closed set of `RegisteredApp`, fixed at construction."""

    def __init__(self, apps: Iterable[RegisteredApp] = ()) -> None:
        found: dict[str, RegisteredApp] = {}
        for app in apps:
            if app.app_id in found or len(found) >= MAX_REGISTERED_APPS:
                raise ValueError("duplicate or too many registered applications")
            found[app.app_id] = app
        self._apps = found

    @classmethod
    def from_config(cls, document: str, *, roots: Iterable[str] | None = None) -> "AppRegistry":
        """Built-ins plus the trusted configuration document. An invalid entry is an error, not a skip:
        a half-applied registry is harder to reason about than a refused one."""
        allowed = tuple(roots) if roots is not None else _trusted_roots()
        apps: list[RegisteredApp] = []
        entries: list[Mapping[str, object]] = []
        if document.strip():
            parsed = json.loads(document)
            if not isinstance(parsed, list):
                raise ValueError("registered applications must be a JSON list")
            if not all(isinstance(item, dict) for item in parsed):
                raise ValueError("registered application entries must be objects")
            entries.extend(parsed)
        apps.extend(validate(entry, roots=allowed) for entry in entries)
        return cls(apps)

    def get(self, app_id: str) -> RegisteredApp:
        app = self._apps.get(app_id)
        if app is None:
            raise DesktopRefusal(DesktopReason.APP_NOT_REGISTERED)
        return app

    def all(self) -> tuple[RegisteredApp, ...]:
        return tuple(self._apps.values())


# There are deliberately NO built-in applications. Every registered application is the user's own trusted
# configuration: a "built-in" Notepad on current Windows is a launcher stub for a packaged app whose real
# process runs from another path, which would make duplicate detection unreliable.
