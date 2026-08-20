#!/usr/bin/env python3
"""Minimal HTTP trigger wrapper for jellyfin-toggl-sync.

Cloud Scheduler sends a POST /sync to invoke a sync cycle.
The service is private (IAM-authenticated); no application-level token needed.
"""

import http.server
import os
import sys

PORT = int(os.environ.get("PORT", 8080))


class SyncHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/sync":
            try:
                from sync import main

                main()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                print(f"[server] Sync failed: {e}", file=sys.stderr, flush=True)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # sync.py handles its own structured logging


if __name__ == "__main__":
    server = http.server.HTTPServer(("", PORT), SyncHandler)
    print(f"[server] Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
