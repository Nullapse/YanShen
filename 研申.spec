# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_submodules


OPTIONAL_EVAL_EXCLUDES = [
    "ragas",
    "datasets",
    "pyarrow",
    "pandas",
    "numpy",
    "scipy",
    "sklearn",
    "matplotlib",
    "PIL",
    "reportlab",
    "pdfminer",
    "pypdfium2",
    "pypdfium2_raw",
    "google.cloud",
    "grpc",
    "hf_xet",
    # Optional local semantic-search acceleration is intentionally excluded
    # from the desktop release.  The app defaults to its portable feature-hash
    # retriever and falls back gracefully when these packages are unavailable.
    "fastembed",
    "onnxruntime",
    "tokenizers",
    "sqlite_vec",
]

AGENT_HIDDENIMPORTS = (
    collect_submodules("langgraph")
    + collect_submodules("langchain")
    + collect_submodules("langchain_core")
    + collect_submodules("langchain_openai")
    + collect_submodules("langgraph.checkpoint.memory")
    + collect_submodules("langgraph.checkpoint.sqlite")
    + collect_submodules("jinja2")
    + collect_submodules("markupsafe")
    + collect_submodules("pydantic")
    + collect_submodules("httpx")
    + collect_submodules("openai")
)

datas = [
    ("static", "static"),
    ("gongkao/web/templates", "gongkao/web/templates"),
    ("templates_data", "templates_data"),
    ("knowledge/manifest.json", "knowledge"),
    ("knowledge/knowledge_cards_v2.jsonl", "knowledge"),
    ("knowledge/shenlun_methodology.jsonl", "knowledge"),
    ("knowledge/saduck_methodology.jsonl", "knowledge"),
    ("data/gongkao_seed.sqlite3", "data"),
    ("assets/app-icon.ico", "assets"),
    ("desktop_host/gongkao_desktop_host.exe", "."),
    ("desktop_host/Microsoft.Web.WebView2.Core.dll", "."),
    ("desktop_host/Microsoft.Web.WebView2.WinForms.dll", "."),
    ("desktop_host/WebView2Loader.dll", "."),
]
if os.path.exists("evals"):
    datas.append(("evals", "evals"))

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("tkinter") + collect_submodules("webview") + AGENT_HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=OPTIONAL_EVAL_EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="研申",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/app-icon.ico",
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="研申",
)
