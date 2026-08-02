from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup


@lru_cache(maxsize=4)
def _environment(resource_root: str) -> Environment:
    root = Path(resource_root)
    template_root = root / "gongkao" / "web" / "templates"
    if not template_root.is_dir():
        template_root = Path(__file__).resolve().parent / "templates"
    return Environment(
        loader=FileSystemLoader(template_root),
        autoescape=select_autoescape(("html", "xml")),
        auto_reload=False,
    )


def render_layout(
    resource_root: str | Path,
    *,
    title: str,
    body: str,
    active: str,
    flashes: list[tuple[str, str]],
    transient_route: bool,
    sidebar_extra: str,
    app_build: str,
    asset_version: str,
    startup_bootstrap: str,
    filter_bootstrap: str,
) -> str:
    template = _environment(str(Path(resource_root).resolve())).get_template("layout.html")
    return template.render(
        title=title,
        body=Markup(body),
        active=active,
        flashes=flashes,
        transient_route=transient_route,
        app_build=app_build,
        asset_version=asset_version,
        sidebar_extra=Markup(sidebar_extra),
        startup_bootstrap=Markup(startup_bootstrap),
        filter_bootstrap=Markup(filter_bootstrap),
        library_active=active in {"papers", "index", "favorites"},
        learning_active=active in {"attempts", "statistics", "notes"},
    )
