"""A small read-only web server, so other screens on your home network can show the news.

    http://<this laptop>:8765/          the reading page (news.html)
    http://<this laptop>:8765/kiosk     the TV / kiosk view (kiosk.html), for the Pi
    http://<this laptop>:8765/news.json the data the kiosk view reads

Only these addresses exist: nothing else in the bot's folder (config, database, logs) can be
fetched. Start it with `python newsbot.py serve`; install_server.ps1 starts it at logon.
"""
import base64
import hashlib
import logging
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

APP_DIR = Path(__file__).resolve().parent
KIOSK = APP_DIR / "kiosk.html"
log = logging.getLogger("newsbot.server")

HTML = "text/html; charset=utf-8"
ROUTES = {  # address -> (which file, content type)
    "/": ("html", HTML),
    "/news.html": ("html", HTML),
    "/kiosk": ("kiosk", HTML),
    "/kiosk.html": ("kiosk", HTML),
    "/news.json": ("json", "application/json; charset=utf-8"),
}


def script_hashes(page):
    """CSP hashes of the page's inline scripts, so only those exact scripts may run."""
    return " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
        for body in re.findall(r"<script>(.*?)</script>", page, re.S))


def make_handler(cfg):
    files = {"html": cfg["html_output"], "json": cfg["json_output"], "kiosk": KIOSK}

    class Handler(BaseHTTPRequestHandler):
        server_version = "newsbot"
        sys_version = ""

        def do_GET(self):
            self.respond(send_body=True)

        def do_HEAD(self):
            self.respond(send_body=False)

        def respond(self, send_body):
            route = ROUTES.get(urlsplit(self.path).path)
            if not route:
                return self.send(404, b"Not found\n", "text/plain; charset=utf-8", send_body)
            which, content_type = route
            try:
                body = files[which].read_bytes()
            except OSError:
                return self.send(503, b"No edition yet. The first one appears after the next "
                                      b"scheduled run.\n", "text/plain; charset=utf-8", send_body)
            headers = {}
            if which == "json":
                headers["Access-Control-Allow-Origin"] = "*"   # a kiosk page saved on the Pi may read it
            if which == "kiosk":
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                    f"connect-src 'self'; script-src {script_hashes(body.decode('utf-8'))}")
            self.send(200, body, content_type, send_body, headers)

        def send(self, status, body, content_type, send_body, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")         # always the latest edition
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def log_message(self, fmt, *args):  # one line per request would flood the log
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # e.g. the TV dropped the connection
        log.debug("Request from %s failed", client_address, exc_info=True)


def make_server(cfg, port, host="0.0.0.0"):
    return Server((host, port), make_handler(cfg))


def lan_address():
    """This computer's address on the home network (no packets are sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def serve(cfg, port):
    server = make_server(cfg, port)
    url = f"http://{lan_address()}:{server.server_port}"
    log.info("Serving the news on your network: reading page %s/  kiosk %s/kiosk", url, url)
    print(f"Reading page: {url}/\nKiosk / TV:   {url}/kiosk\n(Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
