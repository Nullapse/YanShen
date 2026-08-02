from pathlib import Path


def _read_files(paths):
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def read_static_scripts(root: Path):
    scripts = [root / "static" / "app.js"]
    scripts.extend(sorted((root / "static" / "js").glob("*.js")))
    return _read_files(scripts)


def read_static_styles(root: Path):
    styles = [root / "static" / "app.css"]
    styles.extend(sorted((root / "static" / "css").glob("*.css")))
    return _read_files(styles)


def read_server_application(root: Path):
    """Read the complete web implementation."""
    web_root = root / "gongkao" / "web"
    sources = [web_root / "runtime.py", web_root / "application.py"]
    sources.extend(sorted((web_root / "controllers").glob("*.py")))
    return _read_files(sources)
