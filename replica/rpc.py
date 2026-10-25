"""Read-only JSON-RPC client for isolated native history verification."""
import json
import os
import selectors
import subprocess
import time


class Client:
    def __init__(self, command, env=None, stderr=None, umask=-1):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env, umask=umask)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.seq = 0
        self.incoming_methods = []

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method, params):
        if method not in ("initialize", "thread/loaded/list", "thread/list", "thread/read", "thread/turns/list", "thread/items/list"):
            raise ValueError("Probe method not allowed: " + method)
        self.seq += 1
        self.send({"id": self.seq, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line)
                if "method" in response:
                    self.incoming_methods.append(response["method"])
                if "method" not in response and response.get("id") == self.seq:
                    return response
            if self.selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(self.process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    raise RuntimeError("app-server transport closed")
                self.buffer += chunk
        raise TimeoutError(method + "; incoming methods=" + str(self.incoming_methods))

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdout.close()
