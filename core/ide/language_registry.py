# -*- coding: utf-8 -*-
"""扩展名 -> LSP languageId -> 语言服务器配置 的映射表。

本模块只描述「某种语言该用什么服务器、怎么启动、初始化选项是什么」，
不关心可执行文件是否真实存在（那是 server_provision 的职责），也不启动进程
（那是 lsp_manager 的职责）。

languageId 直接透传给 textDocument/didOpen，必须与语言服务器的约定一致，
不能为了好读而改名；LSP 里 jsx / tsx 是独立取值，不归一成 javascript。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerSpec:
    """一个语言服务器的静态配置。"""

    server_id: str
    language_ids: tuple[str, ...]
    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    initialization_options: dict = field(default_factory=dict)
    root_markers: tuple[str, ...] = ()


SPECS: dict[str, ServerSpec] = {
    "jedi": ServerSpec(
        server_id="jedi",
        language_ids=("python",),
        command=("jedi-language-server",),
        args=(),
        initialization_options={
            "diagnostics": {"enable": True, "didOpen": True, "didChange": True},
            "workspace": {"extraPaths": []},
        },
        root_markers=("pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json", ".git"),
    ),
    "typescript": ServerSpec(
        server_id="typescript",
        language_ids=("javascript", "javascriptreact", "typescript", "typescriptreact"),
        command=("typescript-language-server",),
        args=("--stdio",),
        initialization_options={
            "preferences": {"quotePreference": "double", "includeInlayParameterNameHints": "none"},
        },
        root_markers=("package.json", "tsconfig.json", "jsconfig.json"),
    ),
    "gopls": ServerSpec(
        server_id="gopls",
        language_ids=("go",),
        command=("gopls",),
        args=(),
        initialization_options={
            "usePlaceholders": False,
            "completeUnimported": True,
            "staticcheck": False,
        },
        root_markers=("go.mod", "go.work"),
    ),
    "rust-analyzer": ServerSpec(
        server_id="rust-analyzer",
        language_ids=("rust",),
        command=("rust-analyzer",),
        args=(),
        initialization_options={
            "checkOnSave": {"enable": False},
            "cargo": {"allFeatures": False},
        },
        root_markers=("Cargo.toml",),
    ),
    "clangd": ServerSpec(
        server_id="clangd",
        language_ids=("c", "cpp"),
        command=("clangd",),
        args=("--background-index=false",),
        initialization_options={
            "clangdFileStatus": True,
            "fallbackFlags": ["-std=c++17"],
        },
        root_markers=("compile_commands.json", "CMakeLists.txt", ".clangd", ".git"),
    ),
    "jdtls": ServerSpec(
        server_id="jdtls",
        language_ids=("java",),
        command=("jdtls",),
        args=(),
        initialization_options={
            "settings": {"java": {"signatureHelp": {"enabled": True}}},
        },
        root_markers=("pom.xml", "build.gradle", "settings.gradle", ".classpath"),
    ),
}

_EXT_LANGUAGE: dict[str, str] = {
    "py": "python", "pyi": "python", "pyw": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascriptreact",
    "ts": "typescript", "mts": "typescript", "cts": "typescript",
    "tsx": "typescriptreact",
    "go": "go",
    "rs": "rust",
    "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "java": "java",
}


def language_id_for_path(path: str) -> str:
    """按扩展名判断 LSP languageId；未登记的类型返回空串。

    只看扩展名，不看内容与 shebang：打开文件是热路径，多读一次文件会带来
    额外的 IO 与编码歧义，收益不足。
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return _EXT_LANGUAGE.get(name[dot + 1:].lower(), "")


def server_id_for_language(language_id: str) -> str:
    for spec in SPECS.values():
        if language_id in spec.language_ids:
            return spec.server_id
    return ""


def get_spec(server_id: str) -> ServerSpec | None:
    return SPECS.get(server_id)


def all_language_servers() -> list[tuple[str, str]]:
    """返回 (language_id, server_id) 列表，供设置页枚举全部语言服务。"""
    pairs: list[tuple[str, str]] = []
    for spec in SPECS.values():
        for language_id in spec.language_ids:
            pairs.append((language_id, spec.server_id))
    return sorted(pairs)


def root_markers_for_language(language_id: str) -> tuple[str, ...]:
    spec = SPECS.get(server_id_for_language(language_id))
    return spec.root_markers if spec else ()
