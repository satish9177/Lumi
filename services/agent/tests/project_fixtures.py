"""Synthetic Node projects for the Milestone 10 S3 tests. Never a real project, never a network install."""

import json
import socket
from pathlib import Path

SERVER_JS = r"""
const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');
const port = Number(process.argv[2]);
fs.writeFileSync('env-dump.json', JSON.stringify(process.env));
// A grandchild, so Stop can be shown to end the whole tree (and only it).
const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
fs.writeFileSync('grandchild.pid', String(child.pid));
http.createServer((req, res) => { res.end(req.url === '/health' ? 'ok' : 'hello'); })
  .listen(port, '127.0.0.1', () => console.log('listening on ' + port + '\u001b[31m in red\u001b[0m'));
"""

EXIT_JS = r"""
const fs = require('fs');
fs.writeFileSync('env-dump.json', JSON.stringify(process.env));
console.log('checked');
process.exit(Number(process.argv[2] || 0));
"""

HANG_JS = "setInterval(() => console.log('still going'), 200);\n"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_project(
    folder: Path,
    *,
    port: int = 0,
    dependencies: bool = False,
    install: bool = True,
    extra_scripts: dict[str, str] | None = None,
    lockfile: bool = True,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "server.js").write_text(SERVER_JS, encoding="utf-8")
    (folder / "exit.js").write_text(EXIT_JS, encoding="utf-8")
    (folder / "hang.js").write_text(HANG_JS, encoding="utf-8")
    scripts = {
        "serve": f"node server.js {port}",
        "check": "node exit.js 0",
        "fail": "node exit.js 3",
        "hang": "node hang.js",
        "tool": "fake-tool --version",
        **(extra_scripts or {}),
    }
    package: dict[str, object] = {"name": "synthetic-lumi-fixture", "version": "1.0.0", "private": True, "scripts": scripts}
    if dependencies:
        package["devDependencies"] = {"fake-tool": "1.0.0"}
    (folder / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
    if lockfile:
        (folder / "package-lock.json").write_text(json.dumps({"name": "synthetic-lumi-fixture", "lockfileVersion": 3}), encoding="utf-8")
    if dependencies and install:
        bin_dir = folder / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "fake-tool.cmd").write_text("@echo fake-tool 1.0.0\r\n", encoding="utf-8")
    return folder
