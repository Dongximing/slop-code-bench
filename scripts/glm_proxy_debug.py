"""Debug proxy that logs full request/response between Claude Code and GLM5 API."""
import http.server
import json
import urllib.request
import urllib.error
import sys
import os
from datetime import datetime

TARGET = "http://1.95.77.23:3000"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3001
LOG_DIR = "/tmp/glm_proxy_logs"
os.makedirs(LOG_DIR, exist_ok=True)

request_count = 0

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global request_count
        request_count += 1
        req_id = request_count

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        # Log request
        try:
            data = json.loads(body)
            req_file = os.path.join(LOG_DIR, f"req_{req_id:04d}.json")
            with open(req_file, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Print summary
            model = data.get('model', '?')
            num_messages = len(data.get('messages', []))
            num_tools = len(data.get('tools', []))
            tool_choice = data.get('tool_choice', 'NOT_SET')
            last_msg = data.get('messages', [{}])[-1]
            last_role = last_msg.get('role', '?')
            last_content = str(last_msg.get('content', ''))[:100]
            print(f"[REQ {req_id}] model={model} msgs={num_messages} tools={num_tools} tool_choice={tool_choice} last={last_role}: {last_content}")

            # Log tool names if present
            if num_tools > 0:
                tool_names = [t.get('function', t).get('name', t.get('name', '?')) for t in data.get('tools', [])]
                print(f"  Tools: {tool_names[:10]}{'...' if len(tool_names) > 10 else ''}")
        except Exception as e:
            print(f"[REQ {req_id}] parse error: {e}")

        # Forward to target
        url = TARGET + self.path
        req = urllib.request.Request(url, data=body, method='POST')
        for key, value in self.headers.items():
            if key.lower() not in ('host', 'content-length'):
                req.add_header(key, value)
        req.add_header('Content-Length', str(len(body)))

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()

                # Log response
                try:
                    resp_data = json.loads(resp_body)
                    resp_file = os.path.join(LOG_DIR, f"resp_{req_id:04d}.json")
                    with open(resp_file, 'w') as f:
                        json.dump(resp_data, f, indent=2, ensure_ascii=False)

                    choices = resp_data.get('choices', [{}])
                    if choices:
                        c = choices[0]
                        finish = c.get('finish_reason', '?')
                        msg = c.get('message', {})
                        has_tc = 'tool_calls' in msg
                        content_len = len(str(msg.get('content', '')))
                        stop_reason = resp_data.get('stop_reason', 'NOT_IN_RESPONSE')
                        print(f"[RESP {req_id}] finish={finish} has_tool_calls={has_tc} content_len={content_len} stop_reason={stop_reason}")
                    else:
                        # Anthropic format
                        sr = resp_data.get('stop_reason', '?')
                        content = resp_data.get('content', [])
                        has_tu = any(c.get('type') == 'tool_use' for c in content) if isinstance(content, list) else False
                        print(f"[RESP {req_id}] stop_reason={sr} has_tool_use={has_tu} content_blocks={len(content) if isinstance(content, list) else '?'}")
                except Exception as e:
                    print(f"[RESP {req_id}] parse error: {e}")

                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in ('transfer-encoding',):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            print(f"[RESP {req_id}] HTTP error: {e.code}")
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

print(f"GLM debug proxy on port {PORT}, logging to {LOG_DIR}")
print(f"Forwarding to {TARGET}")
print(f"Change agent base_url to http://127.0.0.1:{PORT}")
server = http.server.HTTPServer(('127.0.0.1', PORT), ProxyHandler)
server.serve_forever()
