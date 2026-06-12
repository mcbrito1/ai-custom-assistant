"""
Hermes Host Agent — roda nativamente no Windows.
Expoe POST http://localhost:9000/exec para o container executar comandos PowerShell.

Uso:
    python host_agent.py

Mantenha este script rodando enquanto quiser usar /run no Telegram.
"""

import json
import logging
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.getenv("HOST_AGENT_SECRET", "hermes-secret-mude-isso")
PORT = int(os.getenv("HOST_AGENT_PORT", "9000"))
CLAUDE_PATH = os.getenv(
    "CLAUDE_CMD_PATH",
    r"C:\Users\b55q12pyifrq67p1\AppData\Roaming\npm\claude.cmd",
)
EXEC_TIMEOUT = int(os.getenv("HOST_AGENT_TIMEOUT", "120"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [host-agent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("host-agent")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info(f"{self.address_string()} - {format % args}")

    def do_POST(self):
        if self.path != "/exec":
            self._respond(404, {"error": "not found"})
            return

        auth = self.headers.get("X-Agent-Secret", "")
        if auth != SECRET:
            log.warning(f"Unauthorized request from {self.address_string()}")
            self._respond(403, {"error": "forbidden"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            command = data.get("command", "")
        except Exception:
            self._respond(400, {"error": "invalid json"})
            return

        if not command:
            self._respond(400, {"error": "missing command"})
            return

        # Resolve 'claude' shorthand to configured full path
        if command.startswith("claude ") or command == "claude":
            resolved = command.replace("claude", f'"{CLAUDE_PATH}"', 1)
        else:
            resolved = command

        log.info(f"Executing: {resolved[:120]}")
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", resolved],
                capture_output=True,
                text=True,
                timeout=EXEC_TIMEOUT,
            )
            output = result.stdout or result.stderr or "(sem saida)"
        except subprocess.TimeoutExpired:
            output = f"Timeout: comando demorou mais de {EXEC_TIMEOUT} segundos."
        except Exception as e:
            output = f"Erro: {e}"

        log.info(f"Output ({len(output)} chars): {output[:80]}")
        self._respond(200, {"output": output})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"Ouvindo em http://0.0.0.0:{PORT}")
    log.info(f"Claude CMD path: {CLAUDE_PATH}")
    log.info(f"Timeout: {EXEC_TIMEOUT}s | Ctrl+C para parar.")
    server.serve_forever()
