# =================================================================================================
# Contributing Authors:	    Brooke Jackson, Myra Jones
# Email Addresses:          bija222@uky.edu, majo286@uky.edu
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

        # Top and bottom walls
        if self.vy < 0 and self.by < WALL_THICKNESS:
            self.vy *= -1
        elif self.vy > 0 and self.by + BALL_SIZE > SCREEN_H - WALL_THICKNESS:
            self.vy *= -1

        if self.leftScore >= WIN_SCORE or self.rightScore >= WIN_SCORE:
            self.over = True
            self.winner = "left" if self.leftScore >= WIN_SCORE else "right"
            winnerPlayer = self.left if self.winner == "left" else self.right
            leaderboard.recordWin(winnerPlayer.initials)

    def snapshot(self) -> dict:
        # Author: Myra Jones
        # Purpose: Build the state message sent to both clients
        # Pre: Caller holds self.lock
        # Post: Returns a dict ready to be JSON-encoded; nothing is modified
        return {"t": "state", "sync": self.sync, "bx": self.bx, "by": self.by,
                "vx": self.vx, "vy": self.vy, "lp": self.left.y, "rp": self.right.y,
                "ls": self.leftScore, "rs": self.rightScore,
                "over": self.over, "winner": self.winner}

    def handleMessage(self, player: Player, message: dict) -> None:
        # Author: Myra Jones
        # Purpose: Apply one message received from a client
        # Pre: message is a decoded JSON object from player
        # Post: Paddle position updated, or player's rematch vote recorded (and the match reset
        #       if both players voted)
        kind = message.get("t")
        with self.lock:
            if kind == "paddle":
                try:
                    player.y = clampPaddleY(int(message.get("y", player.y)))
                except (TypeError, ValueError):
                    pass
            elif kind == "again" and self.over:
                player.wantsAgain = True
                if self.left.wantsAgain and self.right.wantsAgain:
                    self.leftScore = self.rightScore = 0
                    self.over = False
                    self.winner = ""
                    self.left.wantsAgain = self.right.wantsAgain = False
                    self.resetBall("left")

    def playerLeft(self, player: Player) -> None:
        # Author: Myra Jones
        # Purpose: Handle a disconnect: tell the other player and stop the game loop
        # Pre: player belongs to this game
        # Post: self.running is False; the opponent has been notified (if still connected)
        with self.lock:
            if not self.running:
                return
            self.running = False
        other = self.right if player is self.left else self.left
        other.send({"t": "opponent_left"})

    def run(self) -> None:
        # Author: Myra Jones
        # Purpose: Game loop; simulate and broadcast state TICKS_PER_SECOND times per second
        # Pre: Both players are connected and have been sent the "start" message
        # Post: Returns after the game is stopped; closes both sockets
        tickLength = 1.0 / TICKS_PER_SECOND
        nextTick = time.monotonic()
        while True:
            with self.lock:
                if not self.running:
                    break
                if not self.over:
                    self.step()
                self.sync += 1
                state = self.snapshot()
            if not (self.left.send(state) and self.right.send(state)):
                self.playerLeft(self.left)
                break
            nextTick += tickLength
            delay = nextTick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nextTick = time.monotonic()      # fell behind; don't try to catch up in a burst
        for p in (self.left, self.right):
            try:
                p.conn.close()
            except OSError:
                pass


class Lobby:
    # Author: Myra Jones
    # Purpose: Pairs waiting players into games, so any number of clients can connect at once
    # Pre: None
    # Post: Each pair of consecutive joiners is placed in its own Game with its own thread

    def __init__(self) -> None:
        # Author: Myra Jones
        # Purpose: Create an empty lobby
        # Pre: None
        # Post: No player is waiting
        self.lock: threading.Lock = threading.Lock()
        self.waiting: Optional[Player] = None

    def join(self, player: Player) -> None:
        # Author: Myra Jones
        # Purpose: Add a player; if someone is already waiting, start a game with them
        # Pre: player is not already in a game or waiting
        # Post: Either player is the waiting player, or a Game thread has been started for the pair
        with self.lock:
            if self.waiting is None:
                self.waiting = player
                return
            first, self.waiting = self.waiting, None
        game = Game(first, player)
        for p in (first, player):
            p.send({"t": "start", "w": SCREEN_W, "h": SCREEN_H, "pw": PADDLE_W, "ph": PADDLE_H,
                    "side": p.side})
        threading.Thread(target=game.run, daemon=True).start()

    def leave(self, player: Player) -> None:
        # Author: Myra Jones
        # Purpose: Remove a player who disconnected while still waiting for an opponent
        # Pre: None
        # Post: If player was the waiting player, the lobby is empty again
        with self.lock:
            if self.waiting is player:
                self.waiting = None


lobby: Lobby = Lobby()


def sanitizeInitials(raw: object) -> str:
    # Author: Myra Jones
    # Purpose: Reduce client-supplied initials to up to 3 uppercase letters/digits
    # Pre: raw is any value
    # Post: Returns a 1-3 character string ("???" if nothing valid was supplied)
    text = "".join(c for c in str(raw).upper() if c.isalnum())[:3]
    return text or "???"


def handleClient(conn: socket.socket, addr: Tuple[str, int]) -> None:
    # Author: Myra Jones
    # Purpose: Thread body for one client: read the join message, then relay messages to its game
    # Pre: conn is a freshly accepted socket
    # Post: Client is removed from the lobby/game and its socket is closed when it disconnects
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    reader = conn.makefile("rb")
    player: Optional[Player] = None
    try:
        line = reader.readline(4096)
        first = json.loads(line) if line else {}
        if first.get("t") != "join":
            return
        player = Player(conn, sanitizeInitials(first.get("initials", "")))
        print(f"[+] {player.initials} connected from {addr[0]}:{addr[1]}")
        lobby.join(player)
        while True:
            line = reader.readline(4096)
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if player.game is not None and isinstance(message, dict):
                player.game.handleMessage(player, message)
    except (OSError, ValueError):
        pass
    finally:
        if player is not None:
            print(f"[-] {player.initials} disconnected")
            lobby.leave(player)
            if player.game is not None:
                player.game.playerLeft(player)
        try:
            conn.close()
        except OSError:
            pass


class LeaderboardHandler(BaseHTTPRequestHandler):
    # Author: Myra Jones
    # Purpose: Serve the win leaderboard as a simple HTML page
    # Pre: Server created with this handler
    # Post: Responds to GET requests with the current leaderboard

    def do_GET(self) -> None:
        # Author: Myra Jones
        # Purpose: Respond to any GET with the leaderboard page
        # Pre: A GET request has been received
        # Post: HTML page written to the client
        rows = "".join(f"<tr><td>{rank}</td><td>{html.escape(name)}</td><td>{wins}</td></tr>"
                       for rank, (name, wins) in enumerate(leaderboard.ranked(), start=1))
        if not rows:
            rows = '<tr><td colspan="3">No games finished yet</td></tr>'
        page = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Pong Leaderboard</title>"
                "<meta http-equiv='refresh' content='5'>"
                "<style>body{font-family:sans-serif;background:#111;color:#eee;text-align:center}"
                "table{margin:2em auto;border-collapse:collapse}"
                "td,th{padding:.4em 1.5em;border-bottom:1px solid #444}</style></head><body>"
                "<h1>Pong Leaderboard</h1><table><tr><th>#</th><th>Player</th><th>Wins</th></tr>"
                f"{rows}</table></body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: object) -> None:
        # Author: Myra Jones
        # Purpose: Silence per-request logging so the server console stays readable
        # Pre: None
        # Post: Nothing is printed
        return


def startLeaderboardServer() -> None:
    # Author: Myra Jones
    # Purpose: Start the leaderboard web server on port 80 (falls back to 8080 if 80 is not allowed)
    # Pre: None
    # Post: A daemon thread is serving HTTP, or a message explains why it could not be started
    for port in (80, 8080):
        try:
            httpd = HTTPServer(("0.0.0.0", port), LeaderboardHandler)
        except OSError as e:
            print(f"Leaderboard: could not bind port {port} ({e})")
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Leaderboard page available on port {port}")
        return
    print("Leaderboard page disabled")


def main() -> None:
    # Author: Myra Jones
    # Purpose: Run the server: listen for clients forever, one thread per client
    # Pre: Optional command-line argument is the TCP port to listen on (default 5000)
    # Post: Does not return until interrupted (Ctrl+C)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen()
    print(f"Pong server listening on port {port}")
    startLeaderboardServer()
    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handleClient, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.close()


if __name__ == "__main__":
    main()
