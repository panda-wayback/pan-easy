#!/usr/bin/env python3
"""Validate the recursive spec.yaml code index.

Usage: python3 check_index.py [--root <repo-root>]
Exit codes: 0 ok, 1 index errors, 2 missing dependency.
"""

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("check_index: 需要 PyYAML，请先执行 `pip install pyyaml`", file=sys.stderr)
    sys.exit(2)

REQUIRED_LISTS = ("modules", "input", "output", "contract", "depends", "tests")
NON_EMPTY = ("contract", "tests")


def default_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def module_id(root: Path, directory: Path) -> str:
    rel = directory.resolve().relative_to(root.resolve()).as_posix()
    return rel or "."


def check_spec(spec: object, where: str, errors: list[str]) -> dict:
    if not isinstance(spec, dict):
        errors.append(f"{where}: 顶层必须是映射")
        return {}
    for key in ("name", "function"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            errors.append(f"{where}: 缺少字段 `{key}`")
    for key in REQUIRED_LISTS:
        if key not in spec:
            errors.append(f"{where}: 缺少字段 `{key}`")
        elif not isinstance(spec[key], list):
            errors.append(f"{where}: `{key}` 必须是列表")
    for key in NON_EMPTY:
        if isinstance(spec.get(key), list) and not spec[key]:
            errors.append(f"{where}: `{key}` 不能为空")
    return spec


def walk(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    errors: list[str] = []
    graph: dict[str, list[str]] = {}
    stack = [root]
    seen: set[str] = set()

    while stack:
        directory = stack.pop()
        mid = module_id(root, directory)
        if mid in seen:
            errors.append(f"{mid}: 被重复登记")
            continue
        seen.add(mid)

        spec_path = directory / "spec.yaml"
        where = spec_path.relative_to(root).as_posix()
        if not spec_path.is_file():
            errors.append(f"{mid}: 缺少 spec.yaml")
            continue

        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{where}: YAML 解析失败：{exc}")
            continue

        spec = check_spec(spec, where, errors)

        for child in spec.get("modules") or []:
            if not isinstance(child, dict) or not child.get("path") or not child.get("function"):
                errors.append(f"{where}: modules 每项须有 `path` 与 `function`")
                continue
            child_dir = directory / child["path"]
            if not child_dir.is_dir():
                errors.append(f"{where}: 子模块目录不存在：{child['path']}")
                continue
            stack.append(child_dir)

        graph[mid] = [
            str(dep).strip().strip("/") or "."
            for dep in spec.get("depends") or []
            if not str(dep).startswith("ext:")
        ]

    for mid, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                errors.append(f"{mid}: depends 指向未登记模块：{dep}")

    errors.extend(find_cycles(graph))
    return graph, errors


def find_cycles(graph: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if state.get(dep) == 1:
                cycle = path[path.index(dep):] + [dep]
                errors.append("依赖成环：" + " → ".join(cycle))
            elif state.get(dep) is None:
                visit(dep, path + [dep])
        state[node] = 2

    for node in graph:
        if state.get(node) is None:
            visit(node, [node])
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()
    root = (args.root or default_root()).resolve()

    graph, errors = walk(root)
    if errors:
        for err in errors:
            print(f"✗ {err}")
        print(f"\ncheck_index: {len(errors)} 个问题")
        return 1
    print(f"check_index: OK（{len(graph)} 个模块）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
