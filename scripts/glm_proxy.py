"""Threaded proxy for GLM5 API."""
import http.server
import socketserver
import json
import urllib.request
import urllib.error
import sys

TARGET = "http://1.95.77.23:3000"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3001

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        url = TARGET + self.path
        req = urllib.request.Request(url, data=body, method='POST')
        for key, value in self.headers.items():
            if key.lower() not in ('host', 'content-length'):
                req.add_header(key, value)
        req.add_header('Content-Length', str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in ('transfer-encoding',):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())

    def do_GET(self):
        url = TARGET + self.path
        req = urllib.request.Request(url)
        for key, value in self.headers.items():
            if key.lower() not in ('host',):
                req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in ('transfer-encoding',):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())

    def log_message(self, format, *args):
        pass

class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

print(f"GLM threaded proxy on port {PORT}, forwarding to {TARGET}")
server = ThreadedServer(('127.0.0.1', PORT), ProxyHandler)
server.serve_forever()
