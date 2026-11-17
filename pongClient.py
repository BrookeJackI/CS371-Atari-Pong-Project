# =================================================================================================
# Contributing Authors:	    Brooke Jackson, Myra Jones
# Email Addresses:          bija222@uky.edu, majo286@uky.edu
# Date:                     11/17/2026
# Purpose:                  Pong game client. Connects to the server, sends the local paddle's
#                           position, and draws the ball, opponent paddle and score the server
#                           sends back. Also provides the start screen and "play again".
# Misc:                     The server is authoritative for the ball and score, so the two clients
#                           can never drift out of sync; the client only owns its own paddle.
# =================================================================================================

import json
import socket
import sys
import threading
import tkinter as tk
from typing import Optional

import pygame

from assets.code.helperCode import *


class NetConnection:
    # Author: Brooke Jackson
    # Purpose: Wraps a socket with newline-delimited JSON send/receive helpers
    # Pre: sock is a connected TCP socket
    # Post: Messages are never split or merged incorrectly (a buffered reader handles framing)

    def __init__(self, sock: socket.socket) -> None:
        # Author: Brooke Jackson
        # Purpose: Set up buffered reading and a send lock around the socket
        # Pre: sock is connected
        # Post: TCP_NODELAY is enabled so small paddle updates are sent immediately
        self.sock: socket.socket = sock
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = sock.makefile("rb")
        self.sendLock: threading.Lock = threading.Lock()

    def send(self, message: dict) -> bool:
        # Author: Brooke Jackson
        # Purpose: Send one JSON message followed by a newline
        # Pre: message is JSON-serializable
        # Post: Returns True on success, False if the connection is broken
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.sendLock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False

    def receive(self) -> Optional[dict]:
        # Author: Brooke Jackson
        # Purpose: Block until one full JSON message arrives
        # Pre: None
        # Post: Returns the decoded message, or None if the connection closed or errored
        while True:
            try:
                line = self.reader.readline(4096)
            except OSError:
                return None
            if not line:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def close(self) -> None:
        # Author: Brooke Jackson
        # Purpose: Close the connection
        # Pre: None
        # Post: Socket is closed (errors ignored)
        try:
            self.sock.close()
        except OSError:
            pass


class SharedState:
    # Author: Brooke Jackson
    # Purpose: Latest server state, written by the receiver thread and read by the game loop
    # Pre: None
    # Post: All access should go through self.lock

    def __init__(self) -> None:
        # Author: Brooke Jackson
        # Purpose: Initialize with an empty state
        # Pre: None
        # Post: state is None until the first update arrives; flags are False
        self.lock: threading.Lock = threading.Lock()
        self.state: Optional[dict] = None
        self.opponentLeft: bool = False
        self.disconnected: bool = False


def receiverThread(conn: NetConnection, shared: SharedState) -> None:
    # Author: Brooke Jackson
    # Purpose: Background thread: read server messages and keep only the newest state
    # Pre: The "start" message has already been consumed
    # Post: shared is updated until the connection ends, then shared.disconnected is True
    while True:
        message = conn.receive()
        if message is None:
            with shared.lock:
                shared.disconnected = True
            return
        with shared.lock:
            if message.get("t") == "state":
                shared.state = message
            elif message.get("t") == "opponent_left":
                shared.opponentLeft = True
