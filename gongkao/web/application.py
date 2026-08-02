"""Compose the built-in HTTP server from grouped page controllers."""

from ..agent_indexer import AgentIndexWorker
from .controllers import (
    AgentController,
    GradingController,
    HomeController,
    LearningController,
    LibraryController,
    PracticeController,
    SettingsController,
)
from .runtime import (
    DB_PATH,
    DEFAULT_APP_CONTEXT,
    ROOT,
    ApplicationContext,
    BaseHTTPRequestHandler,
    Path,
    ThreadingHTTPServer,
    dispatch_get,
    dispatch_post,
    json,
    logging,
    mimetypes,
    parse_qs,
    prepare_user_database,
    quote,
    safe_static_path,
    seed_db_path,
    unquote,
    urlparse,
    user_db_path,
)


class Handler(
    HomeController,
    LibraryController,
    LearningController,
    SettingsController,
    AgentController,
    PracticeController,
    GradingController,
    BaseHTTPRequestHandler,
):
    @property
    def app_context(self):
        server = getattr(self, "server", None)
        context = getattr(server, "app_context", None)
        if context is not None:
            return context
        if Path(DB_PATH) != DEFAULT_APP_CONTEXT.db_path:
            return ApplicationContext.create(DB_PATH, ROOT)
        return DEFAULT_APP_CONTEXT

    @property
    def db_path(self):
        return self.app_context.db_path

    @property
    def resource_root(self):
        return self.app_context.resource_root

    def log_message(self, format, *args):
        logging.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if dispatch_get(self, path, query):
            return
        if path.startswith("/templates/"):
            self.serve_file(
                self.resource_root / "templates_data" / Path(path.removeprefix("/templates/")).name, download=True
            )
        elif path.startswith("/static/"):
            static_path = safe_static_path(self.resource_root / "static", path.removeprefix("/static/"))
            if static_path is None:
                self.send_error(404)
            else:
                self.serve_file(static_path)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not dispatch_post(self, path):
            self.send_error(404)

    def send_html(self, content, status=200):
        raw = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, content, filename=None, mime="text/plain; charset=utf-8"):
        raw = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def serve_file(self, path, download=False):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # ES module imports are not versioned per-file; force revalidation so a
        # WebView2 cache can never mix JS/CSS from older builds with new HTML.
        self.send_header("Cache-Control", "no-cache")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        self.wfile.write(data)


class LoggingHTTPServer(ThreadingHTTPServer):
    index_worker = None

    def handle_error(self, request, client_address):
        logging.exception("Request failed for %s", client_address)

    def server_close(self):
        worker = self.index_worker
        self.index_worker = None
        if worker is not None:
            worker.stop()
        super().server_close()


def create_server(host="127.0.0.1", port=5000, db_path=None):
    resolved_db_path = Path(db_path) if db_path else user_db_path()
    prepare_user_database(resolved_db_path, seed_db_path())
    server = LoggingHTTPServer((host, port), Handler)
    server.app_context = ApplicationContext.create(resolved_db_path, ROOT)
    if db_path is None:
        server.index_worker = AgentIndexWorker(resolved_db_path)
        server.index_worker.start()
    return server


def run(host="127.0.0.1", port=5000, db_path=None):
    server = create_server(host, port, db_path)
    actual_port = server.server_address[1]
    print(f"研申已启动：http://{host}:{actual_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
