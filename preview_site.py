"""Rebuild and live-reload the generated site during local development."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DEPLOY_DIR = ROOT / "deploy"
HOST = "0.0.0.0"
PORT = 8000
POLL_INTERVAL_SECONDS = 0.5

BUILD_COMMANDS = {
    "tools": [sys.executable, "create_tool_landing_page.py", "dev_config.json"],
    "catalog": [sys.executable, "create_catalog_landing_page.py", "dev_config.json"],
    "index": [sys.executable, "create_site_index_page.py", "dev_config.json"],
}

RELOAD_SCRIPT = b"""<script>
(() => {
  let version;
  async function checkForUpdate() {
    try {
      const response = await fetch('/__preview_version__', { cache: 'no-store' });
      const nextVersion = await response.text();
      if (version === undefined) version = nextVersion;
      else if (version !== nextVersion) window.location.reload();
    } catch (_) {}
  }
  setInterval(checkForUpdate, 200);
  checkForUpdate();
})();
</script>
"""


class PreviewState:
    def __init__(self):
        self.version = 0
        self.lock = threading.Lock()

    def updated(self):
        with self.lock:
            self.version += 1

    def current_version(self):
        with self.lock:
            return str(self.version)


STATE = PreviewState()


def build(targets, tool_models=None):
    """Run the selected generators and report whether all succeeded."""
    print(f"\nBuilding: {', '.join(targets)}", flush=True)
    for target in targets:
        command = BUILD_COMMANDS[target]
        if target == "tools" and tool_models:
            command = command + tool_models
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            print(f"Build failed while generating {target}; watching for another change.", flush=True)
            return False
    STATE.updated()
    print("Build complete.", flush=True)
    return True


def watched_files():
    paths = set(ROOT.glob("templates/*.html"))
    paths.update(ROOT.glob("model_list_dir/*.json"))
    paths.update(ROOT.glob("deploy/static/**/*"))
    paths.update(
        ROOT / name
        for name in (
            "create_tool_landing_page.py",
            "create_catalog_landing_page.py",
            "create_site_index_page.py",
            "dev_config.json",
            "models_all.json",
        )
    )
    return {path for path in paths if path.is_file()}


def snapshot():
    return {path: path.stat().st_mtime_ns for path in watched_files()}


def targets_for(changed_paths):
    targets = set()
    for path in changed_paths:
        relative = path.relative_to(ROOT)
        if relative.parts[:2] == ("deploy", "static"):
            continue
        if relative == Path("templates/catalog_landing_page.html"):
            targets.add("catalog")
        elif relative == Path("templates/site_index_template.html"):
            targets.add("index")
        elif relative == Path("templates/tool_landing_page.html"):
            targets.add("tools")
        elif relative.parts[0] == "model_list_dir":
            targets.update(BUILD_COMMANDS)
        else:
            targets.update(BUILD_COMMANDS)

    if any(path.name == "models_all.json" for path in changed_paths):
        targets.update(BUILD_COMMANDS)
    return [target for target in BUILD_COMMANDS if target in targets]


def tool_models_for(changed_paths):
    build_inputs = []
    for path in changed_paths:
        relative = path.relative_to(ROOT)
        if relative.parts[:2] != ("deploy", "static"):
            build_inputs.append(relative)

    if build_inputs and all(path.parts[0] == "model_list_dir" for path in build_inputs):
        return sorted(path.stem for path in build_inputs)
    return None


def watch():
    previous = snapshot()
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        current = snapshot()
        changed = {
            path
            for path in previous.keys() | current.keys()
            if previous.get(path) != current.get(path)
        }
        previous = current
        if not changed:
            continue

        print("Changed: " + ", ".join(str(path.relative_to(ROOT)) for path in sorted(changed)), flush=True)
        targets = targets_for(changed)
        if targets:
            build(targets, tool_models_for(changed))
        else:
            STATE.updated()
            print("Static files updated.", flush=True)
        previous = snapshot()


class PreviewHandler(SimpleHTTPRequestHandler):
    def log_message(self, message_format, *args):
        if urlparse(self.path).path != "/__preview_version__":
            super().log_message(message_format, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/__preview_version__":
            body = STATE.current_version().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        file_path = Path(self.translate_path(self.path))
        if file_path.is_dir():
            file_path /= "index.html"
        elif not file_path.suffix and file_path.with_suffix(".html").is_file():
            file_path = file_path.with_suffix(".html")
        if file_path.suffix.lower() == ".html" and file_path.is_file():
            body = file_path.read_bytes()
            marker = b"</body>"
            body = body.replace(marker, RELOAD_SCRIPT + marker, 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        super().do_GET()


def main():
    DEPLOY_DIR.mkdir(exist_ok=True)
    build(list(BUILD_COMMANDS))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    handler = partial(PreviewHandler, directory=str(DEPLOY_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Preview available at http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPreview stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
