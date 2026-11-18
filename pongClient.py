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


# This is the main game loop.
def playGame(screenWidth: int, screenHeight: int, paddleWidth: int, paddleHeight: int,
             playerPaddle: str, client: NetConnection) -> None:
    # Author: Brooke Jackson
    # Purpose: Run the game window: handle input, send our paddle to the server, and draw the
    #          ball/opponent/score from the newest server state
    # Pre: client is connected and has received the "start" message for this game
    # Post: Exits the program when the window is closed

    # Pygame inits
    pygame.mixer.pre_init(44100, -16, 2, 2048)
    pygame.init()

    # Constants
    WHITE = (255, 255, 255)
    clock = pygame.time.Clock()
    scoreFont = pygame.font.Font("./assets/fonts/pong-score.ttf", 32)
    winFont = pygame.font.Font("./assets/fonts/visitor.ttf", 48)
    hintFont = pygame.font.Font("./assets/fonts/visitor.ttf", 24)
    pointSound = pygame.mixer.Sound("./assets/sounds/point.wav")
    bounceSound = pygame.mixer.Sound("./assets/sounds/bounce.wav")

    # Display objects
    screen = pygame.display.set_mode((screenWidth, screenHeight))
    pygame.display.set_caption(f"Pong - {playerPaddle} paddle")
    topWall = pygame.Rect(-10, 0, screenWidth + 20, 10)
    bottomWall = pygame.Rect(-10, screenHeight - 10, screenWidth + 20, 10)
    centerLine = []
    for i in range(0, screenHeight, 10):
        centerLine.append(pygame.Rect((screenWidth / 2) - 5, i, 5, 5))

    # Paddle properties and init
    paddleStartPosY = (screenHeight / 2) - (paddleHeight / 2)
    leftPaddle = Paddle(pygame.Rect(10, paddleStartPosY, paddleWidth, paddleHeight))
    rightPaddle = Paddle(pygame.Rect(screenWidth - 20, paddleStartPosY, paddleWidth, paddleHeight))
    ball = pygame.Rect(screenWidth // 2, screenHeight // 2, 5, 5)

    if playerPaddle == "left":
        opponentPaddleObj = rightPaddle
        playerPaddleObj = leftPaddle
    else:
        opponentPaddleObj = leftPaddle
        playerPaddleObj = rightPaddle

    lScore = 0
    rScore = 0
    sync = 0                    # Frame counter; the server is authoritative so no resync is needed
    sentAgain = False           # True once we've voted to play again this round
    lastVel = (0, 0)
    lastScores = (0, 0)

    shared = SharedState()
    threading.Thread(target=receiverThread, args=(client, shared), daemon=True).start()

    while True:
        # Wiping the screen
        screen.fill((0, 0, 0))

        # Snapshot the newest server state
        with shared.lock:
            state = shared.state
            opponentLeft = shared.opponentLeft
            disconnected = shared.disconnected
        gameOver = bool(state and state["over"])

        # Getting keypress events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                client.close()
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_DOWN:
                    playerPaddleObj.moving = "down"
                elif event.key == pygame.K_UP:
                    playerPaddleObj.moving = "up"
                elif event.key == pygame.K_SPACE and gameOver and not sentAgain \
                        and not opponentLeft and not disconnected:
                    client.send({"t": "again"})
                    sentAgain = True
            elif event.type == pygame.KEYUP:
                playerPaddleObj.moving = ""

        # Move our own paddle locally (instant response), then tell the server where it is
        if playerPaddleObj.moving == "down":
            if playerPaddleObj.rect.bottomleft[1] < screenHeight - 10:
                playerPaddleObj.rect.y += playerPaddleObj.speed
        elif playerPaddleObj.moving == "up":
            if playerPaddleObj.rect.topleft[1] > 10:
                playerPaddleObj.rect.y -= playerPaddleObj.speed
        client.send({"t": "paddle", "y": int(playerPaddleObj.rect.y)})

        # Apply the server's view of the ball, opponent paddle and score
        if state:
            ball.x, ball.y = state["bx"], state["by"]
            lScore, rScore = state["ls"], state["rs"]
            opponentPaddleObj.rect.y = state["rp"] if playerPaddle == "left" else state["lp"]

            if (lScore, rScore) != lastScores:
                pointSound.play()
            elif (state["vx"], state["vy"]) != lastVel and not gameOver:
                bounceSound.play()
            lastScores = (lScore, rScore)
            lastVel = (state["vx"], state["vy"])
            if not gameOver:
                sentAgain = False

        # Drawing
        if opponentLeft or disconnected:
            msg = "Opponent left" if opponentLeft else "Connection lost"
            surf = winFont.render(msg, False, WHITE, (0, 0, 0))
            rect = surf.get_rect(center=(screenWidth / 2, screenHeight / 2))
            screen.blit(surf, rect)
        elif gameOver:
            winText = "Player 1 Wins! " if state["winner"] == "left" else "Player 2 Wins! "
            surf = winFont.render(winText, False, WHITE, (0, 0, 0))
            rect = surf.get_rect(center=(screenWidth / 2, screenHeight / 2))
            screen.blit(surf, rect)
            hint = "Waiting for opponent..." if sentAgain else "Press SPACE to play again"
            hintSurf = hintFont.render(hint, False, WHITE, (0, 0, 0))
            screen.blit(hintSurf, hintSurf.get_rect(center=(screenWidth / 2, screenHeight / 2 + 50)))
        else:
            pygame.draw.rect(screen, WHITE, ball)

        for i in centerLine:
            pygame.draw.rect(screen, WHITE, i)
        for paddle in [playerPaddleObj, opponentPaddleObj]:
            pygame.draw.rect(screen, WHITE, paddle)
        pygame.draw.rect(screen, WHITE, topWall)
        pygame.draw.rect(screen, WHITE, bottomWall)
        updateScore(lScore, rScore, screen, WHITE, scoreFont)
        pygame.display.flip()
        clock.tick(60)
        sync += 1


def joinServer(ip: str, port: str, initials: str, errorLabel: tk.Label, app: tk.Tk) -> None:
    # Author: Brooke Jackson
    # Purpose: Fired when the Join button is clicked: connect, wait for an opponent, start the game
    # Pre: ip/port/initials are the raw strings from the start-screen entry boxes
    # Post: On success the start window is closed and the game runs; on failure errorLabel
    #       explains what went wrong and the start screen stays open
    def showError(text: str) -> None:
        # Author: Brooke Jackson
        # Purpose: Show a message in the start window
        # Pre: None
        # Post: errorLabel displays text
        errorLabel.config(text=text)
        errorLabel.update()

    try:
        portNumber = int(port)
    except ValueError:
        showError("Port must be a number")
        return

    showError("Connecting...")
    try:
        sock = socket.create_connection((ip, portNumber), timeout=5)
    except OSError as e:
        showError(f"Could not connect: {e}")
        return
    sock.settimeout(None)
    conn = NetConnection(sock)

    cleaned = "".join(c for c in initials.upper() if c.isalnum())[:3]
    if not conn.send({"t": "join", "initials": cleaned or "???"}):
        showError("Connection lost")
        return

    showError("Connected. Waiting for an opponent... (this window will freeze until one joins)")
    start = conn.receive()
    while start is not None and start.get("t") != "start":
        start = conn.receive()
    if start is None:
        showError("Server closed the connection")
        conn.close()
        return

    app.withdraw()
    playGame(start["w"], start["h"], start["pw"], start["ph"], start["side"], conn)
    app.quit()


# This displays the opening screen, you don't need to edit this (but may if you like)
def startScreen() -> None:
    # Author: Brooke Jackson
    # Purpose: Show the window where the player enters the server IP, port and initials
    # Pre: assets/images/logo.png exists
    # Post: Runs the tk main loop until the window is closed
    app = tk.Tk()
    app.title("Server Info")

    image = tk.PhotoImage(file="./assets/images/logo.png")

    titleLabel = tk.Label(image=image)
    titleLabel.grid(column=0, row=0, columnspan=2)

    ipLabel = tk.Label(text="Server IP:")
    ipLabel.grid(column=0, row=1, sticky="W", padx=8)

    ipEntry = tk.Entry(app)
    ipEntry.grid(column=1, row=1)

    portLabel = tk.Label(text="Server Port:")
    portLabel.grid(column=0, row=2, sticky="W", padx=8)

    portEntry = tk.Entry(app)
    portEntry.grid(column=1, row=2)

    initialsLabel = tk.Label(text="Initials (max 3):")
    initialsLabel.grid(column=0, row=3, sticky="W", padx=8)

    initialsEntry = tk.Entry(app)
    initialsEntry.grid(column=1, row=3)

    errorLabel = tk.Label(text="")
    errorLabel.grid(column=0, row=5, columnspan=2)

    joinButton = tk.Button(text="Join", command=lambda: joinServer(
        ipEntry.get(), portEntry.get(), initialsEntry.get(), errorLabel, app))
    joinButton.grid(column=0, row=4, columnspan=2)

    app.mainloop()


if __name__ == "__main__":
    startScreen()
