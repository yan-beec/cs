"""浏览器启动的 Native Messaging host；标准输出仅写协议帧。"""

from __future__ import annotations

import json
import os
from multiprocessing.connection import Client
import struct
import sys
import threading

from .bridge import EXTENSION_ORIGIN, MAX_MESSAGE, bridge_key, pipe_address, receive_json, send_json


def read_message(stream) -> dict | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("Incomplete message header")
    size = struct.unpack("<I", header)[0]
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Invalid message size")
    payload = bytearray()
    while len(payload) < size:
        chunk = stream.read(size - len(payload))
        if not chunk:
            raise ValueError("Incomplete message")
        payload.extend(chunk)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Message must be an object")
    return value


def write_message(stream, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ValueError("Message is too large")
    stream.write(struct.pack("<I", len(payload)) + payload)
    stream.flush()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != EXTENSION_ORIGIN:
        return
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        connection = Client(pipe_address(), family="AF_PIPE", authkey=bridge_key())
        send_json(connection, {"type": "hello", "origin": EXTENSION_ORIGIN})
    except (OSError, KeyError, ValueError):
        write_message(sys.stdout.buffer, {"type": "offline"})
        return

    stopped = threading.Event()

    def outgoing():
        try:
            while not stopped.is_set():
                if connection.poll(0.2):
                    message = receive_json(connection)
                    if not stopped.is_set():
                        write_message(sys.stdout.buffer, message)
        except (OSError, EOFError, ValueError):
            if not stopped.is_set():
                try:
                    write_message(sys.stdout.buffer, {"type": "offline"})
                except OSError:
                    pass

    output_thread = threading.Thread(target=outgoing, name="native-host-output", daemon=True)
    output_thread.start()
    try:
        while True:
            message = read_message(sys.stdin.buffer)
            if message is None:
                break
            send_json(connection, message)
    except (OSError, EOFError, ValueError):
        pass
    finally:
        stopped.set()
        output_thread.join(timeout=2)
        connection.close()


if __name__ == "__main__":
    main()
