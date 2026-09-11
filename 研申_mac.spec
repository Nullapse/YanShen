# -*- mode: python ; coding: utf-8 -*-

import os
import sys
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

MACOS_HIDDENIMPORTS = (
    collect_submodules("webview")
    + collect_submodules("objc")
    + collect_submodules("WebKit")
    + collect_submodules("AppKit")
    + collect_submodules("Foundation")
)

ENCODING_HIDDENIMPORTS = [
    "encodings",
    "encodings.utf_8",
    "encodings.utf_8_sig",
    "encodings.idna",
    "encodings.gbk",
    "encodings.gb2312",
    "encodings.gb18030",
    "encodings.ascii",
    "encodings.latin_1",
]

datas = [
    ("static", "static"),
    ("gongkao/web/templates", "gongkao/web/templates"),
    ("templates_data", "templates_data"),
    ("knowledge/manifest.json", "knowledge"),
    ("knowledge/knowledge_cards_v2.jsonl", "knowledge"),
    ("knowledge/shenlun_methodology.jsonl", "knowledge"),
    ("knowledge/saduck_methodology.jsonl", "knowledge"),
    ("knowledge/master_methodology.jsonl", "knowledge"),
    ("data/gongkao_seed.sqlite3", "data"),
    ("assets/app-icon.png", "assets"),
    ("assets/app-icon.icns", "assets"),
]
if os.path.exists("evals"):
    datas.append(("evals", "evals"))

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=MACOS_HIDDENIMPORTS + AGENT_HIDDENIMPORTS + ENCODING_HIDDENIMPORTS,
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
    icon="assets/app-icon.icns",
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

app = BUNDLE(
    coll,
    name="研申.app",
    icon="assets/app-icon.icns",
    bundle_identifier="com.yanshen.app",
    info_plist={
        "CFBundleName": "研申",
        "CFBundleDisplayName": "研申",
        "CFBundleGetInfoString": "研申 - 智能申论备考与批改评测系统",
        "CFBundleIdentifier": "com.yanshen.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.education",
    },
)
