"""Facts about a registered project and the pinned Node.js, read by verified handle (Milestone 10 S3).

Nothing here executes anything. Every file is opened read-only, proven by handle (final path,
containment, single link) and hashed; directories are walked with `lstat` and a reparse point is refused.
"""

import os
import stat
from dataclasses import dataclass

from app.domain.projects import LOCKFILES, ExecutableFacts, PackageFacts, ProjectRefusal, first_command, needs_bin, parse_package
from app.files.broker import FileBrokerRefusal, _known_folder, read_verified, resolve
from app.files.handles import is_reparse_point

#: The Program Files known folder: writable only by administrators, unlike a per-user Node install.
PROGRAM_FILES_GUID = "{905E63B6-C1BF-494E-B29C-65B732D3D21A}"
NODE_COMPONENTS = ("nodejs", "node.exe")
NPM_CLI_COMPONENTS = ("nodejs", "node_modules", "npm", "bin", "npm-cli.js")
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_PACKAGE_BYTES = 1024 * 1024
_MAX_LOCKFILE_BYTES = 32 * 1024 * 1024


def program_files() -> str:
    folder = _known_folder(PROGRAM_FILES_GUID)
    if not folder:
        raise ProjectRefusal("node_not_found")
    return folder


def _pinned(base: str, components: tuple[str, ...]) -> ExecutableFacts:
    try:
        path = resolve(base, components)
        verified = read_verified(path, expected_final=path, max_bytes=_MAX_EXECUTABLE_BYTES, root_canonical=base)
    except FileBrokerRefusal:
        raise ProjectRefusal("node_not_found") from None
    return ExecutableFacts(
        path=path, volume=verified.identity.volume, index=verified.identity.index, size=verified.identity.size, sha256=verified.sha256
    )


def pinned_node(base: str | None = None) -> tuple[ExecutableFacts, ExecutableFacts]:
    """`node.exe` and `npm-cli.js` under Program Files, by identity and SHA-256. Never looked up on PATH."""
    root = base or program_files()
    return _pinned(root, NODE_COMPONENTS), _pinned(root, NPM_CLI_COMPONENTS)


def package_facts(root_canonical: str) -> PackageFacts:
    try:
        path = resolve(root_canonical, ("package.json",))
        package = read_verified(path, expected_final=path, max_bytes=_MAX_PACKAGE_BYTES, root_canonical=root_canonical)
    except FileBrokerRefusal as refusal:
        raise ProjectRefusal("package_json_missing" if refusal.code == "file_missing" else "package_json_unreadable") from None
    scripts, has_dependencies, runnable = parse_package(package.data)
    lock_name: str | None = None
    lock_sha: str | None = None
    for name in LOCKFILES:
        try:
            lock_path = resolve(root_canonical, (name,))
        except FileBrokerRefusal as refusal:
            if refusal.code == "file_missing":
                continue
            raise ProjectRefusal("lockfile_unreadable") from None
        try:
            lock = read_verified(lock_path, expected_final=lock_path, max_bytes=_MAX_LOCKFILE_BYTES, root_canonical=root_canonical)
        except FileBrokerRefusal:
            raise ProjectRefusal("lockfile_unreadable") from None
        lock_name, lock_sha = name, lock.sha256
        break
    npmrc_sha: str | None = None
    try:
        npmrc_path = resolve(root_canonical, (".npmrc",))
    except FileBrokerRefusal as refusal:
        if refusal.code != "file_missing":
            raise ProjectRefusal("npmrc_unreadable") from None
    else:
        try:
            npmrc_sha = read_verified(npmrc_path, expected_final=npmrc_path, max_bytes=_MAX_PACKAGE_BYTES, root_canonical=root_canonical).sha256
        except FileBrokerRefusal:
            raise ProjectRefusal("npmrc_unreadable") from None
    return PackageFacts(
        package_sha256=package.sha256, lockfile_name=lock_name, lockfile_sha256=lock_sha, npmrc_sha256=npmrc_sha, scripts=scripts,
        has_dependencies=has_dependencies, runnable_names=runnable,
    )


def ancestor_npm_context(root_canonical: str) -> str | None:
    """Anything above the project that npm would use. npm walks up from the working directory: a parent
    `package.json` whose `workspaces` include the project becomes npm's project prefix (its `.npmrc` applies
    and `workspace=` can run ANOTHER package's script), and every ancestor's `node_modules/.bin` is put on
    PATH. None of that is part of the recipe, so a project with any of it above it is refused (S3 review
    findings 1 and 9)."""
    current = os.path.dirname(root_canonical.rstrip("\\/"))
    while current and os.path.dirname(current) != current:
        for name in ("package.json", "node_modules", ".npmrc"):
            try:
                os.lstat(os.path.join(current, name))
            except FileNotFoundError:
                continue
            except OSError:
                return name
            return name
        current = os.path.dirname(current)
    return None


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    ok: bool
    missing: str | None = None


def _exists_plain(path: str, *, directory: bool) -> bool:
    try:
        facts = os.lstat(path)
    except OSError:
        return False
    if directory:
        return stat.S_ISDIR(facts.st_mode) and not is_reparse_point(facts)
    return stat.S_ISREG(facts.st_mode) or is_reparse_point(facts)  # npm's .bin entries may be links


def check_dependencies(root_canonical: str, facts: PackageFacts, script_texts: list[str]) -> DependencyCheck:
    """Are the project's dependencies present? Read-only: Lumi never installs anything."""
    modules = os.path.join(root_canonical, "node_modules")
    commands = [first_command(text) for text in script_texts]
    if (facts.has_dependencies or any(needs_bin(command) for command in commands)) and not _exists_plain(modules, directory=True):
        return DependencyCheck(False, "node_modules")
    for command in commands:
        if not needs_bin(command):
            continue
        assert command is not None
        bin_dir = os.path.join(modules, ".bin")
        if not any(_exists_plain(os.path.join(bin_dir, name), directory=False) for name in (f"{command}.cmd", command)):
            return DependencyCheck(False, command)
    return DependencyCheck(True)
