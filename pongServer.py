# =================================================================================================
# Contributing Authors:	    Brooke Jackson, Myra Jones
# Email Addresses:          person1@uky.edu, majo286@uky.edu
# Date:                     2026-11-18
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


class Player:
    # Author: Myra Jones
    # Purpose: One connected client: its socket, identity, paddle position and game membership
    # Pre: conn is a connected TCP socket
    # Post: send() is safe to call from multiple threads

    def __init__(self, conn: socket.socket, initials: str) -> None:
        # Author: Myra Jones
        # Purpose: Create a player record
        # Pre: conn is connected; initials is sanitized
        # Post: Player is not yet in a game (game is None, side is "")
        self.conn: socket.socket = conn
        self.initials: str = initials
        self.side: str = ""
        self.y: int = clampPaddleY(SCREEN_H // 2 - PADDLE_H // 2)
        self.wantsAgain: bool = False
        self.game: Optional["Game"] = None
        self.sendLock: threading.Lock = threading.Lock()

    def send(self, message: dict) -> bool:
        # Author: Myra Jones
        # Purpose: Send one JSON message terminated by a newline
        # Pre: message is JSON-serializable
        # Post: Returns True if sent; False if the connection is broken
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.sendLock:
                self.conn.sendall(data)
            return True
        except OSError:
            return False


class Game:
    # Author: Brooke Jackson, Myra Jones
    # Purpose: Owns the state of one match between two players and simulates it at a fixed tick rate
    # Pre: Constructed with exactly two Players (left first, right second)
    # Post: run() returns once the game has been stopped (a player left)

    def __init__(self, left: Player, right: Player) -> None:
        # Author: Myra Jones
        # Purpose: Set up a fresh match
        # Pre: left and right are distinct, connected Players
        # Post: Players are assigned sides and linked to this game; scores are 0; ball is centered
        self.left: Player = left
        self.right: Player = right
        left.side, right.side = "left", "right"
        left.game = self
        right.game = self
        self.lock: threading.Lock = threading.Lock()
        self.running: bool = True
        self.sync: int = 0
        self.leftScore: int = 0
        self.rightScore: int = 0
        self.over: bool = False
        self.winner: str = ""
        self.bx: int = SCREEN_W // 2
        self.by: int = SCREEN_H // 2
        self.vx: int = -BALL_SPEED
        self.vy: int = 0

    def resetBall(self, nowGoing: str) -> None:
        # Author: Myra Jones
        # Purpose: Put the ball back at center heading toward the given side
        # Pre: nowGoing is "left" or "right"; caller holds self.lock
        # Post: Ball is centered with horizontal velocity toward nowGoing and no vertical velocity
        self.bx, self.by = SCREEN_W // 2, SCREEN_H // 2
        self.vx = -BALL_SPEED if nowGoing == "left" else BALL_SPEED
        self.vy = 0

    def step(self) -> None:
        # Author: Brooke Jackson, Myra Jones
        # Purpose: Advance the simulation one tick: move ball, score, paddle and wall bounces
        # Pre: Caller holds self.lock and the game is not over
        # Post: Ball position/velocity and scores are updated; may set over/winner and record a win
        self.bx += self.vx
        self.by += self.vy

        # Scoring (same rules as the original single-player demo)
        if self.bx > SCREEN_W:
            self.leftScore += 1
            self.resetBall("left")
        elif self.bx < 0:
            self.rightScore += 1
            self.resetBall("right")

        # Paddles. Only bounce if the ball is travelling toward the paddle, so it can't get stuck.
        leftX, rightX = 10, SCREEN_W - 20
        for player, paddleX in ((self.left, leftX), (self.right, rightX)):
            movingToward = self.vx < 0 if player is self.left else self.vx > 0
            if movingToward and rectsOverlap(self.bx, self.by, BALL_SIZE, BALL_SIZE,
                                             paddleX, player.y, PADDLE_W, PADDLE_H):
                self.vx *= -1
                self.vy = ((self.by + BALL_SIZE // 2) - (player.y + PADDLE_H // 2)) // 2
