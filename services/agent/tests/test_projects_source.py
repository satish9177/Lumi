"""Milestone 10 S3 source pins: exactly one project spawn, with a fixed argv shape and no shell, anywhere.

A regression that gave Lumi a second way to start a project, a shell, a terminal, `npm install` or Git
shows up here as a failing test, not as a quiet capability."""

import ast
import inspect
from pathlib import Path

from app.api import project_routes
from app.projects import process

APP = Path(__file__).resolve().parents[1] / "app"
PROJECT_FILES = [*sorted((APP / "projects").glob("*.py")), APP / "services" / "projects.py", APP / "domain" / "projects.py",
                 APP / "api" / "project_routes.py", APP / "repositories" / "projects.py"]


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value.id if isinstance(func.value, ast.Name) else "?"
        return f"{base}.{func.attr}"
    return func.id if isinstance(func, ast.Name) else "?"


def test_there_is_exactly_one_project_spawn_and_it_never_uses_a_shell() -> None:
    spawns = []
    for path in PROJECT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            name = _name(call)
            assert name not in ("os.system", "os.startfile", "os.popen", "os.execv", "os.execve", "os.spawnv", "subprocess.run",
                                "subprocess.call", "subprocess.check_call", "subprocess.check_output", "asyncio.create_subprocess_shell",
                                "asyncio.create_subprocess_exec"), f"{path.name}: {name}"
            if name == "subprocess.Popen":
                spawns.append((path.name, call))
    assert [where for where, _ in spawns] == ["process.py"]
    call = spawns[0][1]
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert ast.unparse(keywords["executable"]) == "spec.node_path"
    assert ast.unparse(call.args[0]) == "spec.argv()"
    assert ast.unparse(keywords["env"]) == "dict(spec.env)"


def test_the_argv_shape_is_fixed() -> None:
    source = inspect.getsource(process.LaunchSpec.argv)
    assert 'return [self.node_path, self.npm_cli_path, "run", self.script]' in source


def _code_strings(tree: ast.AST) -> list[str]:
    """Every string constant that is code, not a docstring."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_no_shell_terminal_install_or_git_anywhere_in_the_project_code() -> None:
    for path in PROJECT_FILES:
        strings = [value.lower() for value in _code_strings(ast.parse(path.read_text(encoding="utf-8")))]
        for value in strings:
            assert "powershell" not in value and "pwsh" not in value, f"{path.name}: {value!r}"
            assert value not in ("install", "ci", "add", "git", "exec", "npx", "/c", "/k"), f"{path.name}: {value!r}"
            assert "sendkeys" not in value and "sendinput" not in value, f"{path.name}: {value!r}"
        # The only mention of the command processor is the ComSpec variable npm itself needs.
        assert [value for value in strings if "cmd.exe" in value] == (["cmd.exe"] if path.name == "projects.py" and path.parent.name == "services" else []), path


def test_no_route_takes_a_command_an_executable_or_arguments() -> None:
    for name, model in vars(project_routes).items():
        if isinstance(model, type) and name.endswith("Body") and hasattr(model, "model_fields"):
            fields = set(model.model_fields)
            assert not fields & {"command", "cmd", "executable", "args", "argv", "cwd", "shell", "program"}, name
            if name == "RegisterProjectBody":
                assert fields == {"path", "label"}


def test_nothing_else_in_the_runtime_spawns_with_a_shell() -> None:
    for path in APP.rglob("*.py"):
        for call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            for keyword in call.keywords:
                if keyword.arg == "shell":
                    assert isinstance(keyword.value, ast.Constant) and keyword.value.value is False, f"{path}: shell=..."
