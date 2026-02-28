"""Simple greeting web app."""

import os
from http.server import HTTPServer, BaseHTTPRequestHandler

GREETING = "Welcome to a test app!"
PORT = int(os.environ.get("PORT", 3000))


class GreetingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "healthy"}')
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = f"""<!DOCTYPE html>
<html>
<head><title>Greeting App</title></head>
<body>
<h1>{GREETING}</h1>
</body>
</html>"""
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), GreetingHandler)
    print(f"Serving on port {PORT}")
    server.serve_forever()
