# =================================================================================================
# Contributing Authors:	    Brooke Jackson, Myra Jones
# Email Addresses:          bija222@uky.edu, majo286@uky.edu
# Date:                     2026-11-17
# Purpose:                  Pong game server. Accepts any number of clients over TCP, pairs them
#                           into two-player games (one thread per client, one game-loop thread per
#                           game), simulates the ball and score authoritatively, relays state to
#                           both players, supports "play again", and serves a win leaderboard
#                           as a webpage over HTTP.
# Misc:                     Protocol is newline-delimited JSON over TCP.
#   client -> server: {"t":"join","initials":"ABC"}
#                     {"t":"paddle","y":215}
#                     {"t":"again"}
#   server -> client: {"t":"start","w":640,"h":480,"pw":10,"ph":50,"side":"left"}
#                     {"t":"state","sync":N,"bx":..,"by":..,"vx":..,"vy":..,"lp":..,"rp":..,
#                      "ls":..,"rs":..,"over":bool,"winner":"left"|"right"|""}
#                     {"t":"opponent_left"}
# =================================================================================================

import html
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple

# ---- Game constants (must agree with what is sent to clients in the "start" message) ------------
SCREEN_W: int = 640
SCREEN_H: int = 480
PADDLE_W: int = 10
PADDLE_H: int = 50
BALL_SIZE: int = 5
WALL_THICKNESS: int = 10
WIN_SCORE: int = 5
TICKS_PER_SECOND: int = 60
BALL_SPEED: int = 5

LEADERBOARD_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.json")


def clampPaddleY(y: int) -> int:
    # Author: Myra Jones
    # Purpose: Keep a paddle's top edge between the top and bottom walls
    # Pre: y is an integer
    # Post: Returns y limited to the playable range; nothing is modified
    return max(WALL_THICKNESS, min(SCREEN_H - WALL_THICKNESS - PADDLE_H, y))


def rectsOverlap(ax: int, ay: int, aw: int, ah: int, bx: int, by: int, bw: int, bh: int) -> bool:
    # Author: Myra Jones
    # Purpose: Axis-aligned rectangle overlap test (same semantics as pygame's colliderect)
    # Pre: All arguments are integers; rects are given as x, y, width, height
    # Post: Returns True if the rectangles overlap; nothing is modified
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


class Leaderboard:
    # Author: Myra Jones
    # Purpose: Thread-safe, file-backed count of wins per set of initials
    # Pre: LEADERBOARD_FILE is in a writable location (if not, wins are kept in memory only)
    # Post: Every recorded win is saved to disk when possible

    def __init__(self, path: str) -> None:
        # Author: Myra Jones
        # Purpose: Load any existing leaderboard from disk
        # Pre: path is a file path (the file need not exist)
        # Post: self.wins holds the loaded counts (empty if the file is missing or corrupt)
        self.path: str = path
        self.lock: threading.Lock = threading.Lock()
        self.wins: Dict[str, int] = {}
        try:
            with open(path, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.wins = {str(k): int(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            self.wins = {}

    def recordWin(self, initials: str) -> None:
        # Author: Myra Jones
        # Purpose: Add one win for the given initials and persist the leaderboard
        # Pre: initials is a short sanitized string
        # Post: self.wins[initials] is incremented and the file is rewritten (errors ignored)
        with self.lock:
            self.wins[initials] = self.wins.get(initials, 0) + 1
            try:
                with open(self.path, "w") as f:
                    json.dump(self.wins, f)
            except OSError:
                pass

    def ranked(self) -> List[Tuple[str, int]]:
        # Author: Myra Jones
        # Purpose: Provide the leaderboard sorted from most to fewest wins
        # Pre: None
        # Post: Returns a new list of (initials, wins); internal state is unchanged
        with self.lock:
            return sorted(self.wins.items(), key=lambda kv: (-kv[1], kv[0]))

leaderboard: Leaderboard = Leaderboard(LEADERBOARD_FILE)

