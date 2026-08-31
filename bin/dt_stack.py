#!/usr/bin/env python3
"""dt-stack — supervise the data-tournaments serving stack.

Components (in start order):
  temporal   Temporal dev server (skipped when already SERVING)
  worker     release workflow worker (temporalio venv)
  ui         Phoenix LiveView UI (mix phx.server via nix develop)

Contract: docs/design/operationalize-v14.md. This is a SUPERVISOR, not a
pipeline executor — the pipeline registry stays a spec; stages remain
orchestrated by their own tools. launchd/systemd wiring is the user's
call: point the unit at `dt_stack.py up` and `dt_stack.py down`.

State lives under $DT_STACK_HOME (default ~/.dt-stack):
  stack.env   editable config (generated with defaults on first `up`;
              VAR NAMES only for anything secret-adjacent — no values)
  home/       persistent DATA_TOURNAMENTS_HOME (unless overridden)
  run/        pidfiles + logs/

Port honesty: a UI port held by a LIVE foreign process => refuse with
the pid. Held by a dead/stuck socket (kernel-stuck listener, the :4111
case) => auto-increment and SAY SO in stack.env + stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STACK_HOME = Path(os.environ.get("DT_STACK_HOME", REPO / ".dt-stack"))
RUN = STACK_HOME / "run"
LOGS = RUN / "logs"
ENV_FILE = STACK_HOME / "stack.env"

DEFAULTS = {
    "DATA_TOURNAMENTS_HOME": str(STACK_HOME / "home"),
    "UI_PORT": "4113",
    "TEMPORAL_PORT": "7233",
    "TEMPORAL_UI_PORT": "8233",
    "RELEASE_TASK_QUEUE": "dt-stack-release",
    "PROMPT_BACKEND": "local",
    "DT_OPERATOR": "changeme",
    "PYTHON": str(REPO / "spikes/temporal-unity-release/.venv/bin/python"),
    "NIX_HOME_UI": "/tmp/nixhome-ui",
    "NIX_SITE_PACKAGES": (
        "/nix/store/iwqsybpv5m0qcrm8br5vam872ggiagqg-python3-3.13.13-env"
        "/lib/python3.13/site-packages"
    ),
    "NIX_BIN": "/run/current-system/sw/bin",
}

COMPONENTS = ("temporal", "worker", "ui")

def load_env() -> dict:
    cfg = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg

def write_env(cfg: dict) -> None:
    STACK_HOME.mkdir(parents=True, exist_ok=True)
    lines = [
        "# dt-stack config — edit values, then `dt_stack.py down && up`.",
        "# No secrets here: anything credential-like stays in the repo-root",
        "# .env (user-owned); this file carries operational knobs only.",
    ]
    lines += [f"{k}={v}" for k, v in cfg.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")

def pidfile(name: str) -> Path:
    return RUN / f"{name}.pid"

def read_pid(name: str):
    try:
        return int(pidfile(name).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None

def pid_alive(pid) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

def port_state(port: int) -> str:
    """'free' | 'live' (something accepts) | 'stuck' (listener exists but
    no live pid — the kernel-stuck-socket case)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return "live"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return "free"
        except OSError:
            return "stuck"

def spawn(name: str, cmd: list, env: dict, cwd=None) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "ab")
    proc = subprocess.Popen(
        cmd, cwd=cwd or str(REPO), env=env,
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pidfile(name).write_text(str(proc.pid))
    return proc.pid

def temporal_healthy(cfg: dict) -> bool:
    try:
        out = subprocess.run(
            [f"{cfg['NIX_BIN']}/nix", "run", "nixpkgs#temporal-cli", "--",
             "operator", "cluster", "health",
             "--address", f"127.0.0.1:{cfg['TEMPORAL_PORT']}"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(STACK_HOME)},
        )
        return "SERVING" in out.stdout
    except Exception:
        return False

def ui_healthy(cfg: dict) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{cfg['UI_PORT']}/", timeout=5
        ) as r:
            return r.status == 200
    except Exception:
        return False

def worker_healthy(cfg: dict) -> bool:
    return pid_alive(read_pid("worker"))

HEALTH = {"temporal": temporal_healthy, "worker": worker_healthy, "ui": ui_healthy}

def base_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env.update(
        DATA_TOURNAMENTS_HOME=cfg["DATA_TOURNAMENTS_HOME"],
        PROMPT_BACKEND=cfg["PROMPT_BACKEND"],
        RELEASE_TASK_QUEUE=cfg["RELEASE_TASK_QUEUE"],
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        PATH=f"{env.get('PATH', '')}:/usr/bin:/bin:{cfg['NIX_BIN']}",
    )
    return env

def up(cfg: dict) -> int:
    STACK_HOME.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        write_env(cfg)
        print(f"wrote {ENV_FILE} (defaults — edit and re-run to change)")
    Path(cfg["DATA_TOURNAMENTS_HOME"]).mkdir(parents=True, exist_ok=True)
    rc = 0

    if temporal_healthy(cfg):
        print("temporal: already SERVING — skipped")
    else:
        spawn("temporal",
              [f"{cfg['NIX_BIN']}/nix", "run", "nixpkgs#temporal-cli", "--",
               "server", "start-dev", "--headless",
               "--port", cfg["TEMPORAL_PORT"],
               "--db-filename", str(STACK_HOME / "temporal.db")],
              {**base_env(cfg), "HOME": str(STACK_HOME)})
        ok = wait_for(lambda: temporal_healthy(cfg), 90)
        print(f"temporal: {'SERVING' if ok else 'FAILED (see logs)'}")
        rc |= 0 if ok else 1

    if worker_healthy(cfg):
        print("worker: already running — skipped")
    else:
        env = base_env(cfg)
        env["PYTHONPATH"] = f"{REPO}:{cfg['NIX_SITE_PACKAGES']}"
        spawn("worker", [cfg["PYTHON"], "-m", "bin.release_workflow.worker"], env)
        time.sleep(3)
        ok = worker_healthy(cfg)
        print(f"worker: {'running (queue ' + cfg['RELEASE_TASK_QUEUE'] + ')' if ok else 'FAILED (see logs)'}")
        rc |= 0 if ok else 1

    port = int(cfg["UI_PORT"])
    if ui_healthy(cfg) and pid_alive(read_pid("ui")):
        print(f"ui: already serving :{port} — skipped")
    else:
        state = port_state(port)
        if state == "live":
            print(f"ui: REFUSED — :{port} is held by a live foreign process; "
                  f"edit UI_PORT in {ENV_FILE} or stop that process")
            return rc | 1
        if state == "stuck":
            old = port
            while port_state(port) != "free":
                port += 1
            print(f"ui: :{old} socket is dead-but-stuck (kernel); moving to :{port}")
            cfg["UI_PORT"] = str(port)
            write_env(cfg)
        env = base_env(cfg)
        env.update(PORT=str(port), HOME=cfg["NIX_HOME_UI"],
                   DT_OPERATOR=cfg["DT_OPERATOR"])
        spawn("ui",
              [f"{cfg['NIX_BIN']}/nix", "develop", "--command", "mix", "phx.server"],
              env, cwd=str(REPO / "ui"))
        ok = wait_for(lambda: ui_healthy(cfg), 120)
        print(f"ui: {'serving http://localhost:' + str(port) if ok else 'FAILED (see logs)'}")
        rc |= 0 if ok else 1
    return rc

def wait_for(fn, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(2)
    return fn()

def status(cfg: dict) -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in COMPONENTS:
        pid = read_pid(name)
        alive = pid_alive(pid)
        healthy = HEALTH[name](cfg)
        port = {"temporal": cfg["TEMPORAL_PORT"], "ui": cfg["UI_PORT"],
                "worker": "-"}[name]
        rows.append({"component": name, "pid": pid if alive else None,
                     "port": port, "healthy": healthy})
        print(f"{name:9s} pid={pid if alive else '-':<8} port={port:<6} "
              f"{'HEALTHY' if healthy else 'DOWN'}")
    (RUN / "status.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(r["healthy"] for r in rows) else 1

def down(cfg: dict) -> int:
    for name in reversed(COMPONENTS):
        pid = read_pid(name)
        if not pid_alive(pid):
            print(f"{name}: not running")
            pidfile(name).unlink(missing_ok=True)
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 15
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(1)
        if pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                os.kill(pid, signal.SIGKILL)
            print(f"{name}: SIGKILL after grace")
        else:
            print(f"{name}: stopped")
        pidfile(name).unlink(missing_ok=True)
    return 0

def logs(cfg: dict, component: str, lines: int) -> int:
    path = LOGS / f"{component}.log"
    if not path.exists():
        print(f"no log for {component} at {path}")
        return 1
    print(f"── {path} (last {lines} lines) ──")
    content = path.read_text(errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)
    return 0

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dt_stack.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    sub.add_parser("status")
    sub.add_parser("down")
    sp = sub.add_parser("logs")
    sp.add_argument("component", choices=COMPONENTS)
    sp.add_argument("-n", "--lines", type=int, default=60)
    args = p.parse_args(argv)
    cfg = load_env()
    if args.cmd == "up":
        return up(cfg)
    if args.cmd == "status":
        return status(cfg)
    if args.cmd == "down":
        return down(cfg)
    return logs(cfg, args.component, args.lines)

if __name__ == "__main__":
    sys.exit(main())
