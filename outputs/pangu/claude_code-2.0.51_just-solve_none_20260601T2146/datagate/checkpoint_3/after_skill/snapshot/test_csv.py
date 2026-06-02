from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import time

csv_data = b"""name,age,city,salary
John,25,NYC,50000.50
Jane,30,LA,60000.75
Bob,35,Chicago,70000
Alice,28,Boston,55000.25
Charlie,40,Miami,80000.00
Dave,22,Seattle,45000
Eve,33,Denver,58000.50
Frank,27,Austin,52000.75
Grace,29,Phoenix,54000.25
Henry,31,Dallas,57000.10"""

class CSVHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/csv')
        self.end_headers()
        self.wfile.write(csv_data)

server = HTTPServer(('127.0.0.1', 8899), CSVHandler)
thread = threading.Thread(target=server.serve_forever)
thread.daemon = True
thread.start()
print("Test CSV server running on http://127.0.0.1:8899/test.csv")
time.sleep(60)  # Keep alive for tests
