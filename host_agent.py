"""
Hermes Host Agent — roda nativamente no Windows.
Expoe POST http://localhost:9000/exec para o container executar comandos PowerShell.

Uso:
    python host_agent.py

Mantenha este script rodando enquanto quiser usar /run no Telegram.
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.getenv("HOST_AGENT_SECRET", "hermes-secret-mude-isso")
PORT = int(os.getenv("HOST_AGENT_PORT", "9000"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[host-agent] {self.address_string()} - {format % args}")

    def do_POST(self):
        if self.path != "/exec":
            self._respond(404, {"error": "not found"})
            return

        auth = self.headers.get("X-Agent-Secret", "")
        if auth != SECRET:
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

        try:
            # Resolve 'claude' shorthand to full path so PowerShell finds it
            resolved = command.replace(
                "claude ",
                r"C:\Users\b55q12pyifrq67p1\AppData\Roaming\npm\claude.cmd ",
                1,
            ) if command.startswith("claude ") or command == "claude" else command

            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", resolved],
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout or result.stderr or "(sem saida)"
        except subprocess.TimeoutExpired:
            output = "Timeout: comando demorou mais de 30 segundos."
        except Exception as e:
            output = f"Erro: {e}"

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
    print(f"[host-agent] Ouvindo em http://0.0.0.0:{PORT}")
    print(f"[host-agent] Secret: {SECRET}")
    print("[host-agent] Ctrl+C para parar.")
    server.serve_forever()
