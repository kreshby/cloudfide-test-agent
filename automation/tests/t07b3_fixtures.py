"""Finite localhost wire responses, using actual HTTP clients and sockets."""

from contextlib import contextmanager
import socketserver
import threading
import time

from automation.test_agent.provider import canonical


class WireAPI:
    def __init__(self, reply, *, framing="length", close=True, extra_length=0, delay=0):
        self.reply = reply
        self.framing, self.close = framing, close
        self.extra_length, self.delay = extra_length, delay
        self.operations = []

    @contextmanager
    def serve(self):
        api = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                method, path, _ = self.rfile.readline().decode("ascii").strip().split(" ")
                headers = {}
                while line := self.rfile.readline().strip():
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                body = self.rfile.read(int(headers.get("content-length", "0")))
                api.operations.append((method, path, body))
                status, value = api.reply(method, path, body)
                if status is None:
                    return  # Close the real socket without any response headers.
                data = value if type(value) is bytes else canonical(value)
                wire = f"HTTP/1.1 {status} Fixture\r\nConnection: {'close' if api.close else 'keep-alive'}\r\n".encode()
                if api.framing == "length":
                    wire += f"Content-Length: {len(data) + api.extra_length}\r\n\r\n".encode() + data
                elif api.framing == "chunked":
                    wire += b"Transfer-Encoding: chunked\r\n\r\n" + f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n"
                else:
                    assert api.close and api.framing == "eof"
                    wire += b"\r\n" + data
                time.sleep(api.delay)
                try:
                    self.wfile.write(wire)
                    self.wfile.flush()
                    if not api.close:
                        self.rfile.read(1)  # Prove framing ends without waiting for peer EOF.
                except (OSError, TimeoutError):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        with Server(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            try:
                yield server.server_address[1]
            finally:
                server.shutdown()
                worker.join(timeout=2)
