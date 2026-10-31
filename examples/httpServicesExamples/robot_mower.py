"""A game for kids: plant grass on a map, and a robot driven by a local agent mows it.

Each turn this service tells the agent what every possible move would lead to, and the agent picks
one. Moves into a wall are never offered. After two moves that don't get closer to the grass, it may
only pick from the moves that do until it cuts the next patch, so the game can't get stuck. The page
counts how often that happens.

    python3.15 -m examples.httpServicesExamples.robot_mower --port 8586
"""

import argparse
import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from brain.sdk import BrainApiError, BrainClient

WIDTH = 8
HEIGHT = 6
MAX_BODY_BYTES = 8192
MAX_STRAYED_MOVES = 2
BRAIN_TIMEOUT_SECONDS = 300.0

DIRECTIONS = frozendict(up=(0, -1), down=(0, 1), left=(-1, 0), right=(1, 0))

SYSTEM = """You drive a lawn mower robot in a children's game. Each turn you see the moves you can
make and what each one leads to. Pick a move that cuts grass. If none does, pick the move that leaves
the nearest grass the fewest steps away."""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robot Mower</title>
<style>
  :root { --sky: #e4f4fb; --ink: #23412f; --muted: #557463; --lawn: #cdeb9f; --lawn-alt: #c1e491;
          --grass: #3f9b45; --grass-tip: #2d7d38; --panel: #fff; --go: #e8622c; --go-ink: #fff;
          --line: #a9cf7c; --bad: #b3261e; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 16px 40px; background: var(--sky); color: var(--ink);
         font: 17px/1.4 "Trebuchet MS", system-ui, sans-serif; }
  main { max-width: 640px; margin: auto; }
  h1 { margin: 0; font-size: 34px; }
  .sub { margin: 2px 0 16px; color: var(--muted); }
  #bubble { min-height: 54px; margin-bottom: 12px; padding: 14px 16px; background: var(--panel);
            border-radius: 14px; font-size: 19px; font-weight: 700; }
  #bubble.error { color: var(--bad); font-size: 16px; }
  #field { position: relative; display: grid; grid-template-columns: repeat(__WIDTH__, 1fr);
           border: 4px solid var(--line); border-radius: 14px; overflow: hidden;
           touch-action: none; user-select: none; }
  .cell { display: block; width: 100%; aspect-ratio: 1; padding: 0; background: var(--lawn);
          border: 0; border-radius: 0; cursor: pointer; }
  .cell.alt { background: var(--lawn-alt); }
  .cell:focus-visible { outline: 3px solid var(--go); outline-offset: -3px; }
  .cell.grass { background-color: var(--grass); background-size: 76%;
    background-position: center; background-repeat: no-repeat;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23a6e57d' stroke-width='2' stroke-linecap='round'%3E%3Cpath d='M5 21c0-5-1-8-3-11M9 21c0-7-1-11-2-15M13 21c0-8 0-13 1-18M17 21c0-6 1-10 3-14M21 21c0-3 0-5 1-7'/%3E%3C/svg%3E"); }
  #robot { position: absolute; top: 0; left: 0; width: calc(100% / __WIDTH__);
           aspect-ratio: 1; padding: 1%; pointer-events: none;
           transition: transform .45s ease-in-out; }
  #robot svg { display: block; width: 100%; height: 100%; transition: transform .2s; }
  #robot.left svg { transform: scaleX(-1); }
  #robot.busy svg { animation: bob .7s ease-in-out infinite alternate; }
  @keyframes bob { to { translate: 0 -5%; } }
  @media (prefers-reduced-motion: reduce) {
    #robot, #robot svg { transition: none; }
    #robot.busy svg { animation: none; }
  }
  .controls { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
  button.action { flex: 1 1 130px; padding: 13px; color: var(--ink); background: var(--panel);
                  border: 0; border-radius: 12px; font: inherit; font-weight: 700; cursor: pointer; }
  button.action.go { flex-grow: 2; color: var(--go-ink); background: var(--go); font-size: 20px; }
  button.action:disabled { opacity: .5; cursor: default; }
  .score { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 14px; }
  .score div { padding: 10px; background: var(--panel); border-radius: 12px; text-align: center;
               font-size: 13px; color: var(--muted); }
  .score b { display: block; color: var(--ink); font-size: 24px; }
  @media (max-width: 460px) { .score { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<main>
  <h1>Robot Mower</h1>
  <p class="sub">Tap or drag on the lawn to plant grass. Then send the robot to mow it!</p>
  <div id="bubble" role="status">Hi! Plant some grass for me!</div>
  <div id="field">
    <div id="robot" aria-hidden="true">
      <svg viewBox="0 0 48 48">
        <line x1="24" y1="10" x2="24" y2="4" stroke="#23412f" stroke-width="3" stroke-linecap="round"/>
        <circle cx="24" cy="4" r="3" fill="#e8622c"/>
        <rect x="8" y="10" width="32" height="24" rx="8" fill="#f5b52e" stroke="#23412f" stroke-width="2.5"/>
        <circle cx="18" cy="21" r="4" fill="#fff" stroke="#23412f" stroke-width="2"/>
        <circle cx="31" cy="21" r="4" fill="#fff" stroke="#23412f" stroke-width="2"/>
        <circle cx="19" cy="21" r="1.7" fill="#23412f"/>
        <circle cx="32" cy="21" r="1.7" fill="#23412f"/>
        <path d="M18 28q6 4 12 0" fill="none" stroke="#23412f" stroke-width="2.5" stroke-linecap="round"/>
        <rect x="5" y="36" width="38" height="5" rx="2.5" fill="#6d7b74"/>
        <circle cx="13" cy="42" r="5" fill="#23412f"/>
        <circle cx="35" cy="42" r="5" fill="#23412f"/>
      </svg>
    </div>
  </div>
  <div class="controls">
    <button class="action go" id="go" type="button">Go, robot!</button>
    <button class="action" id="meadow" type="button">Surprise lawn</button>
    <button class="action" id="clear" type="button">Clear</button>
  </div>
  <div class="score">
    <div><b id="cut">0</b>grass cut</div>
    <div><b id="moves">0</b>moves</div>
    <div><b id="own">0</b>robot's own choices</div>
    <div><b id="helped">0</b>helper stepped in</div>
  </div>
</main>
<script>
  const WIDTH = __WIDTH__;
  const HEIGHT = __HEIGHT__;
  const field = document.getElementById("field");
  const robotElement = document.getElementById("robot");
  const bubble = document.getElementById("bubble");
  const goButton = document.getElementById("go");
  const numbers = Object.fromEntries(
    ["cut", "moves", "own", "helped"].map((id) => [id, document.getElementById(id)]),
  );

  const key = (x, y) => `${x},${y}`;
  const cells = new Map();
  const grass = new Set();
  const score = { cut: 0, moves: 0, own: 0, helped: 0 };
  let robot = [0, 0];
  let strayed = 0;
  let running = null;

  for (let y = 0; y < HEIGHT; y++) {
    for (let x = 0; x < WIDTH; x++) {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = (x + y) % 2 ? "cell alt" : "cell";
      cell.dataset.x = x;
      cell.dataset.y = y;
      field.insertBefore(cell, robotElement);
      cells.set(key(x, y), cell);
    }
  }

  function paint(x, y, planted) {
    if (planted && x === robot[0] && y === robot[1]) return;
    const id = key(x, y);
    if (planted) grass.add(id);
    else grass.delete(id);
    const cell = cells.get(id);
    cell.classList.toggle("grass", planted);
    cell.setAttribute("aria-label", `Column ${x + 1}, row ${y + 1}: ${planted ? "grass" : "short lawn"}`);
  }

  for (const [id, cell] of cells) {
    const [x, y] = id.split(",").map(Number);
    paint(x, y, false);
  }

  function say(text, isError = false) {
    bubble.textContent = text;
    bubble.className = isError ? "error" : "";
  }

  function placeRobot() {
    const [x, y] = robot;
    robotElement.style.transform = `translate(${x * 100}%, ${y * 100}%)`;
  }

  function showScore() {
    for (const [name, element] of Object.entries(numbers)) element.textContent = score[name];
  }

  // Dragging plants or removes depending on the first cell touched, on both mouse and touch.
  let brush = null;
  const cellAt = (event) => document.elementFromPoint(event.clientX, event.clientY)?.closest(".cell");

  field.addEventListener("pointerdown", (event) => {
    const cell = cellAt(event);
    if (!cell) return;
    event.preventDefault();
    brush = !cell.classList.contains("grass");
    paint(Number(cell.dataset.x), Number(cell.dataset.y), brush);
  });
  field.addEventListener("pointermove", (event) => {
    if (brush === null) return;
    const cell = cellAt(event);
    if (cell) paint(Number(cell.dataset.x), Number(cell.dataset.y), brush);
  });
  window.addEventListener("pointerup", () => { brush = null; });
  window.addEventListener("pointercancel", () => { brush = null; });
  field.addEventListener("click", (event) => {
    // A pointer has already painted by now. Only the keyboard reaches here with no detail.
    const cell = event.target.closest(".cell");
    if (cell && event.detail === 0) {
      paint(Number(cell.dataset.x), Number(cell.dataset.y), !cell.classList.contains("grass"));
    }
  });

  document.getElementById("clear").addEventListener("click", () => {
    for (const id of [...grass]) {
      const [x, y] = id.split(",").map(Number);
      paint(x, y, false);
    }
    if (!running) say("All clear! Plant some grass for me!");
  });

  document.getElementById("meadow").addEventListener("click", () => {
    for (let planted = 0; planted < 6; planted++) {
      paint(Math.floor(Math.random() * WIDTH), Math.floor(Math.random() * HEIGHT), true);
    }
    if (!running) say("Wow, what a lawn! Press Go when you're ready.");
  });

  const WORDS = { up: "Going up!", down: "Going down!", left: "Going left!", right: "Going right!" };

  async function askForMove(signal) {
    const response = await fetch("/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal,
      body: JSON.stringify({
        robot,
        strayed,
        grass: [...grass].map((id) => id.split(",").map(Number)),
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error);
    return body;
  }

  function apply(decision) {
    robot = decision.robot;
    strayed = decision.strayed;
    robotElement.classList.toggle("left", decision.move === "left");
    placeRobot();
    score.moves += 1;
    score[decision.helped ? "helped" : "own"] += 1;
    if (grass.has(key(...robot))) {
      paint(robot[0], robot[1], false);
      score.cut += 1;
      say("Snip snip! Yummy grass!");
    } else {
      say(WORDS[decision.move]);
    }
    showScore();
  }

  function setRunning(controller) {
    running = controller;
    goButton.textContent = controller ? "Stop" : "Go, robot!";
    robotElement.classList.toggle("busy", Boolean(controller));
  }

  goButton.addEventListener("click", async () => {
    if (running) {
      running.abort();
      return;
    }
    if (grass.size === 0) {
      say("I need grass to cut. Plant some first!");
      return;
    }
    const controller = new AbortController();
    setRunning(controller);
    strayed = 0;
    let first = true;
    try {
      while (grass.size > 0) {
        if (first) say("Thinking about my first move…");
        const decision = await askForMove(controller.signal);
        first = false;
        apply(decision);
      }
      say("All done! The lawn looks great! Plant more?");
    } catch (error) {
      if (error.name === "AbortError") say("Okay, I'll wait here.");
      else say(error.message || "I lost contact with my brain.", true);
    } finally {
      setRunning(null);
    }
  });

  placeRobot();
</script>
</body>
</html>
""".replace("__WIDTH__", str(WIDTH)).replace("__HEIGHT__", str(HEIGHT)).encode()


class InvalidBoard(Exception):
    pass


type Cell = tuple[int, int]


def _distance(cell: Cell, grass: frozenset[Cell]) -> int:
    return min(abs(cell[0] - x) + abs(cell[1] - y) for x, y in grass)


@dataclass(frozen=True, slots=True, kw_only=True)
class Board:
    robot: Cell
    grass: frozenset[Cell]
    strayed: int

    def moves(self) -> dict[str, Cell]:
        """Every move that stays on the map, and the cell each one reaches."""
        x, y = self.robot
        reachable = {name: (x + dx, y + dy) for name, (dx, dy) in DIRECTIONS.items()}
        return {name: cell for name, cell in reachable.items() if _on_map(cell)}

    def closer_moves(self) -> list[str]:
        here = _distance(self.robot, self.grass)
        return [
            name for name, cell in self.moves().items() if _distance(cell, self.grass) < here
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    move: str
    robot: Cell
    helped: bool
    strayed: int


def _on_map(cell: Cell) -> bool:
    return 0 <= cell[0] < WIDTH and 0 <= cell[1] < HEIGHT


class RobotDriver:
    """Turns a board into the next move, talking to the brain only through its public API."""

    def __init__(self, brain: BrainClient, agent: str | None) -> None:
        self._brain = brain
        self._agent = agent
        self._lock = threading.Lock()
        self._total_moves = 0
        self._total_cut = 0

    def next_move(self, board: Board) -> Decision:
        moves = board.moves()
        closer = board.closer_moves()
        coached = board.strayed >= MAX_STRAYED_MOVES
        allowed = closer if coached else list(moves)
        choice = self._ask_agent(board, allowed)
        helped = coached or choice is None
        move = choice or closer[0]
        reached = moves[move]
        # Only cutting clears this. Otherwise one good move between bad ones could loop forever.
        strayed = 0 if reached in board.grass else board.strayed + (move not in closer)
        self._report(cut=reached in board.grass)
        return Decision(move=move, robot=reached, helped=helped, strayed=strayed)

    def _ask_agent(self, board: Board, allowed: list[str]) -> str | None:
        reply = self._brain.chat(
            self._agent or self._pick_agent(),
            message=_describe(board, allowed),
            system=SYSTEM,
            format={
                "type": "object",
                "properties": {"move": {"type": "string", "enum": allowed}},
                "required": ["move"],
            },
            think=False,
            options={"temperature": 0, "num_predict": 20},
            auto_mount=True,
        )
        try:
            move = json.loads(reply["content"])["move"]
        except KeyError, TypeError, ValueError:
            return None
        return move if move in allowed else None

    def _report(self, *, cut: bool) -> None:
        with self._lock:
            self._total_moves += 1
            self._total_cut += cut
            moves, grass_cut = self._total_moves, self._total_cut
        self._brain.report(
            "robot-mower", name="Robot Mower", kind="game", moves=moves, grass_cut=grass_cut
        )

    def _pick_agent(self) -> str:
        talkable = [
            agent
            for agent in self._brain.agents()
            if agent.get("can_chat") and agent.get("installed")
        ]
        talkable.sort(key=lambda agent: not agent.get("mounted"))
        if not talkable:
            raise BrainApiError(409, "no_agent", "No installed agent can chat yet.")
        self._agent = talkable[0]["name"]
        return self._agent


def _describe(board: Board, allowed: list[str]) -> str:
    lines = []
    for name in allowed:
        cell = board.moves()[name]
        if cell in board.grass:
            lines.append(f"- {name}: cuts grass")
        else:
            lines.append(f"- {name}: nearest grass is then {_distance(cell, board.grass)} steps away")
    return "Your moves:\n" + "\n".join(lines) + "\nPick the best move."


def _cell(value: object) -> Cell:
    match value:
        case [int() as x, int() as y] if _on_map((x, y)):
            return x, y
    raise InvalidBoard("Every position must be a column and row on the map.")


def parse_board(raw: bytes) -> Board:
    try:
        body = json.loads(raw)
        robot = _cell(body["robot"])
        grass = frozenset(_cell(item) for item in body["grass"])
        strayed = body["strayed"]
    except KeyError, TypeError, ValueError:
        raise InvalidBoard("Send the robot, the grass and the strayed count.") from None
    if not isinstance(strayed, int) or strayed < 0:
        raise InvalidBoard("The strayed count must not be negative.")
    grass -= {robot}
    if not grass:
        raise InvalidBoard("There is no grass left to cut.")
    return Board(robot=robot, grass=grass, strayed=strayed)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: GameServer

    def do_GET(self) -> None:
        if self.path != "/":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self) -> None:
        if self.path != "/move":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        # Browsers won't send this content type across origins without asking first.
        if self.headers.get("Content-Type") != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send JSON."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "The request size is not acceptable."})
            return
        try:
            decision = self.server.driver.next_move(parse_board(self.rfile.read(length)))
        except InvalidBoard as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (BrainApiError, OSError) as error:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"The brain could not answer: {error}"})
        else:
            self._send_json(
                HTTPStatus.OK,
                {
                    "move": decision.move,
                    "robot": decision.robot,
                    "helped": decision.helped,
                    "strayed": decision.strayed,
                },
            )

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError, ConnectionResetError:
            self.close_connection = True  # The player pressed Stop while the agent was deciding.


class GameServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], driver: RobotDriver) -> None:
        self.driver = driver
        super().__init__(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8586)
    parser.add_argument("--brain", default="http://127.0.0.1:8484")
    parser.add_argument("--agent", help="Which agent drives. Picks an installed one by default.")
    args = parser.parse_args()

    driver = RobotDriver(BrainClient(args.brain, timeout=BRAIN_TIMEOUT_SECONDS), args.agent)
    with GameServer(("127.0.0.1", args.port), driver) as server:
        print(f"Robot Mower is at http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
