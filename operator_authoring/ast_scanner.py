from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .model import CallableInfo, CallableKind, CatalogWarning, ClassInfo, ParameterInfo, ProjectCatalog

EXCLUDED_DIRS = {
    ".git", ".idea", ".vscode", ".qoder", "__pycache__", ".ipynb_checkpoints",
    ".pytest_cache", ".mypy_cache", "venv", ".venv", "site-packages", "dist", "build",
}

NAME_WEIGHTS = {
    "run": 100, "process": 95, "predict": 90, "transform": 85, "execute": 80,
    "convert": 80, "calculate": 80, "handle": 75, "infer": 75, "main": 20,
}


def _safe_unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _literal_value(node: ast.AST | None) -> tuple[bool, Any | None]:
    if node is None:
        return False, None
    try:
        value = ast.literal_eval(node)
        json.dumps(value)
        return True, value
    except Exception:
        return False, None


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _module_name(python_root: Path, file_path: Path) -> str:
    relative = file_path.relative_to(python_root)
    if relative.name == "__init__.py":
        relative = relative.parent
    else:
        relative = relative.with_suffix("")
    return ".".join(relative.parts)


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in operator workspace: {relative}")
        if path.is_file():
            yield path


def source_revision(root: str | Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for path in _iter_source_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _parameter_infos(arguments: ast.arguments, *, drop_first: bool) -> list[ParameterInfo]:
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults_offset = len(positional) - len(arguments.defaults)
    result: list[ParameterInfo] = []

    for index, argument in enumerate(positional):
        if drop_first and index == 0 and argument.arg in {"self", "cls"}:
            continue
        default_node = arguments.defaults[index - defaults_offset] if index >= defaults_offset else None
        has_default, default_value = _literal_value(default_node)
        kind = "POSITIONAL_ONLY" if index < len(arguments.posonlyargs) else "POSITIONAL_OR_KEYWORD"
        result.append(ParameterInfo(
            name=argument.arg, kind=kind, annotation=_safe_unparse(argument.annotation),
            required=default_node is None, has_default=has_default,
            default_value=default_value, default_repr=_safe_unparse(default_node),
        ))

    if arguments.vararg is not None:
        result.append(ParameterInfo(
            name=arguments.vararg.arg, kind="VAR_POSITIONAL",
            annotation=_safe_unparse(arguments.vararg.annotation), required=False,
        ))

    for argument, default_node in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        has_default, default_value = _literal_value(default_node)
        result.append(ParameterInfo(
            name=argument.arg, kind="KEYWORD_ONLY", annotation=_safe_unparse(argument.annotation),
            required=default_node is None, has_default=has_default,
            default_value=default_value, default_repr=_safe_unparse(default_node),
        ))

    if arguments.kwarg is not None:
        result.append(ParameterInfo(
            name=arguments.kwarg.arg, kind="VAR_KEYWORD",
            annotation=_safe_unparse(arguments.kwarg.annotation), required=False,
        ))

    return result


def _callable_score(name: str, decorators: list[str]) -> int:
    score = NAME_WEIGHTS.get(name.lower(), 10)
    if name.startswith("_"):
        score -= 25
    if any(item in {"operator", "dsc.operator", "dsc_sdk.operator"} for item in decorators):
        score += 100
    return score


def _method_kind(decorators: list[str]) -> CallableKind:
    if "staticmethod" in decorators:
        return CallableKind.STATIC_METHOD
    if "classmethod" in decorators:
        return CallableKind.CLASS_METHOD
    return CallableKind.INSTANCE_METHOD


def _valid_module_name(value: str) -> bool:
    return bool(value) and all(part.isidentifier() for part in value.split("."))


def scan_project(project_root: str | Path, *, python_path: str = ".") -> ProjectCatalog:
    project = Path(project_root).resolve()
    python_root = (project / python_path).resolve()

    try:
        python_root.relative_to(project)
    except ValueError as exc:
        raise ValueError("python_path escapes the workspace") from exc
    if not python_root.is_dir():
        raise ValueError(f"python_path does not exist: {python_path}")

    functions: list[CallableInfo] = []
    classes: list[ClassInfo] = []
    warnings: list[CatalogWarning] = []

    for path in sorted(python_root.rglob("*.py")):
        relative_to_project = path.relative_to(project)
        if any(part in EXCLUDED_DIRS for part in relative_to_project.parts):
            continue
        if path.name.startswith("test_") or path.is_symlink():
            continue

        module = _module_name(python_root, path)
        if not _valid_module_name(module):
            warnings.append(CatalogWarning(
                severity="warning", file=relative_to_project.as_posix(),
                message=f"file is not importable from python_path={python_path!r}; module={module!r}",
            ))
            continue

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            warnings.append(CatalogWarning(
                severity="error", file=relative_to_project.as_posix(),
                line=getattr(exc, "lineno", None), message=str(exc),
            ))
            continue

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [_decorator_name(item) for item in node.decorator_list]
                callable_id = f"{module}:{node.name}"
                functions.append(CallableInfo(
                    id=callable_id, module=module, qualname=node.name, kind=CallableKind.FUNCTION,
                    file=relative_to_project.as_posix(), line=node.lineno,
                    is_async=isinstance(node, ast.AsyncFunctionDef), decorators=decorators,
                    score=_callable_score(node.name, decorators),
                    parameters=_parameter_infos(node.args, drop_first=False),
                    return_annotation=_safe_unparse(node.returns),
                ))
                continue

            if not isinstance(node, ast.ClassDef):
                continue

            class_target = f"{module}:{node.name}"
            constructor: list[ParameterInfo] = []
            methods: list[CallableInfo] = []

            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = [_decorator_name(item) for item in child.decorator_list]

                if child.name == "__init__":
                    constructor = _parameter_infos(child.args, drop_first=True)
                    continue
                if child.name.startswith("__") and child.name.endswith("__"):
                    continue

                kind = _method_kind(decorators)
                drop_first = kind in {CallableKind.INSTANCE_METHOD, CallableKind.CLASS_METHOD}
                qualname = f"{node.name}.{child.name}"
                methods.append(CallableInfo(
                    id=f"{module}:{qualname}", module=module, qualname=qualname, kind=kind,
                    class_target=class_target, method_name=child.name,
                    file=relative_to_project.as_posix(), line=child.lineno,
                    is_async=isinstance(child, ast.AsyncFunctionDef), decorators=decorators,
                    score=_callable_score(child.name, decorators),
                    parameters=_parameter_infos(child.args, drop_first=drop_first),
                    return_annotation=_safe_unparse(child.returns),
                ))

            classes.append(ClassInfo(
                id=class_target, module=module, qualname=node.name, target=class_target,
                file=relative_to_project.as_posix(), line=node.lineno,
                constructor=constructor, methods=methods,
            ))

    return ProjectCatalog(
        source_revision=source_revision(project),
        python_path=python_path,
        functions=sorted(functions, key=lambda item: (-item.score, item.id)),
        classes=sorted(classes, key=lambda item: item.id),
        warnings=warnings,
    )
