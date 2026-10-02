"""Operational dashboard API for Chat Model Relay.

The admin API intentionally exposes only non-secret runtime state. Mutating
operations are explicit and reuse the relay's existing persistence and browser
coordination primitives.
"""

from __future__ import annotations
import httpx
import subprocess
import socket

import asyncio
import os
import resource
import signal
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.api.browser_gate import get_tab_pool
from src.api.conversation_store import ConversationStore
from src.api.observability import metrics
from src.config import Config

router = APIRouter(tags=["admin"])
_STARTED_AT = time.time()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_DIR = _PROJECT_ROOT / "logs"


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class ServiceAction(BaseModel):
    confirm: bool = False


_SETTING_DEFS: dict[str, dict[str, Any]] = {
    # Core & Provider
    "PROVIDER": {"type": "choice", "choices": list(Config.SUPPORTED_PROVIDERS), "restart": True, "category": "Core & Relay", "description": "Active upstream model provider (chatgpt, gemini, claude, minimax)."},
    "API_HOST": {"type": "text", "restart": True, "category": "Core & Relay", "description": "Address the relay server binds to (default: 127.0.0.1)."},
    "API_PORT": {"type": "int", "min": 1, "max": 65535, "restart": True, "category": "Core & Relay", "description": "HTTP port for the relay API (e.g. 8650 for ChatGPT, 8651 for Gemini)."},
    "HEADLESS": {"type": "bool", "restart": True, "category": "Browser Automation", "description": "Run browser without a visible window (set to false for interactive login)."},
    
    # Browser & Concurrency
    "MAX_CONCURRENT_REQUESTS": {"type": "int", "min": 1, "max": 64, "restart": True, "category": "Browser Automation", "description": "Maximum concurrent browser requests allowed."},
    "MAX_ACTIVE_TABS": {"type": "int", "min": 1, "max": 64, "restart": True, "category": "Browser Automation", "description": "Maximum browser tabs maintained in pool."},
    "BROWSER_CHANNEL": {"type": "choice", "choices": ["chrome", "chromium", "msedge"], "restart": True, "category": "Browser Automation", "description": "Browser distribution used by Playwright engine."},
    "SLOW_MO": {"type": "int", "min": 0, "max": 2000, "restart": True, "category": "Browser Automation", "description": "Milliseconds to slow down browser operations (for visual debugging)."},

    # Provider URLs
    "CHATGPT_URL": {"type": "text", "restart": True, "category": "Provider URLs", "description": "Base URL for ChatGPT web interface (default: https://chatgpt.com)."},
    "CHATGPT_PROJECT_URL": {"type": "text", "restart": True, "category": "Provider URLs", "description": "Optional ChatGPT project workspace URL for isolated project chats."},
    "GEMINI_URL": {"type": "text", "restart": True, "category": "Provider URLs", "description": "Base URL for Gemini web interface (default: https://gemini.google.com/app)."},
    "CLAUDE_URL": {"type": "text", "restart": True, "category": "Provider URLs", "description": "Base URL for Claude web interface (default: https://claude.ai)."},

    # Model Tuning
    "CHATGPT_DEFAULT_MODEL": {"type": "text", "restart": True, "category": "Model Tuning", "description": "Default ChatGPT model to switch to on start (e.g. gpt-4o, gpt-5.5)."},
    "CHATGPT_MODEL_SWITCH_TIMEOUT": {"type": "int", "min": 1000, "max": 60000, "restart": True, "category": "Model Tuning", "description": "Timeout in milliseconds when switching models in UI."},
    "CHATGPT_MODEL_SWITCH_STRICT": {"type": "bool", "restart": True, "category": "Model Tuning", "description": "Fail request if requested model is not found in UI picker."},
    "CHATGPT_LONG_PROMPT_FALLBACK": {"type": "choice", "choices": ["attachment", "none"], "restart": True, "category": "Model Tuning", "description": "How to handle oversized prompts (attachment upload vs direct error)."},

    # Context & Storage
    "API_PROJECT_THREAD_MAX_CHARS": {"type": "int", "min": 10000, "max": 2000000, "restart": True, "category": "Context & Storage", "description": "Max stored transcript chars before starting a fresh thread."},
    "API_PROJECT_THREAD_CONTEXT_CHARS": {"type": "int", "min": 1000, "max": 100000, "restart": True, "category": "Context & Storage", "description": "Chars of recent conversation context injected on new requests."},
    "API_CONVERSATION_RETENTION_SECONDS": {"type": "int", "min": 60, "max": 31536000, "restart": True, "category": "Context & Storage", "description": "How long conversation routes are retained in SQLite database."},
    "API_CONVERSATION_MAX_ROUTES": {"type": "int", "min": 100, "max": 1000000, "restart": True, "category": "Context & Storage", "description": "Maximum number of stored conversation routes."},

    # VNC & Remote GUI
    "RELAY_VNC_PASSWORD": {"type": "text", "restart": True, "category": "VNC & Remote GUI", "description": "Password for accessing the container noVNC Web GUI (port 5800/5801) and direct VNC stream."},

    # Security
    "RELAY_API_KEY": {"type": "text", "restart": True, "category": "Security", "description": "Bearer token required for API authorization (optional on localhost)."},
    "API_TOKEN_OPTIONAL": {"type": "bool", "restart": True, "category": "Security", "description": "Allow unauthenticated localhost requests without bearer token."},
}


def _probe_vnc_status() -> dict[str, Any]:
    def is_open(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.04)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False
        finally:
            s.close()

    p5800 = is_open(5800)
    p5801 = is_open(5801)
    available = p5800 or p5801
    return {
        "available": available,
        "chatgpt_open": p5800,
        "gemini_open": p5801,
        "mode": "docker" if available else "native_macos",
        "ports": {"chatgpt": 5800, "gemini": 5801},
        "direct_ports": {"chatgpt": 5900, "gemini": 5901},
        "status_text": "Online (Docker noVNC Web)" if available else "Offline (Native Mac Chrome in use)",
    }

async def _get_companion_info() -> dict[str, Any]:
    is_chatgpt = Config.PROVIDER == "chatgpt"
    c_port = 8651 if is_chatgpt else 8650
    c_id = "gemini" if is_chatgpt else "chatgpt"
    c_name = "Gemini" if is_chatgpt else "ChatGPT"
    c_models = ["gemini-browser"] if is_chatgpt else ["gpt-4o", "catgpt-browser"]
    companion_host = "catgpt-gemini" if is_chatgpt else "catgpt"
    urls = [
        f"http://127.0.0.1:{c_port}/admin/overview?shallow=true",
        f"http://{companion_host}:8000/admin/overview?shallow=true",
    ]

    async with httpx.AsyncClient(timeout=1.0) as client:
        for url in urls:
            try:
                res = await client.get(url)
            except httpx.HTTPError:
                continue
            if res.status_code == 200:
                data = res.json()
                svc = data.get("service", {})
                q = data.get("queue", {})
                conv = data.get("conversations", {})
                return {
                    "id": c_id,
                    "name": c_name,
                    "port": c_port,
                    "status": "online",
                    "models": c_models,
                    "memory_bytes": svc.get("memory_bytes", 0),
                    "active": q.get("active", 0),
                    "capacity": q.get("capacity", 3),
                    "sessions": conv.get("sessions", 0),
                }

    return {
        "id": c_id,
        "name": c_name,
        "port": c_port,
        "status": "offline",
        "models": c_models,
        "memory_bytes": 0,
        "active": 0,
        "capacity": 3,
        "sessions": 0,
    }



_COMPANION_PROCESS = None

@router.post("/admin/companion/{provider_id}/start")
async def start_companion(provider_id: str) -> dict[str, Any]:
    global _COMPANION_PROCESS
    if provider_id not in ("gemini", "chatgpt"):
        raise HTTPException(status_code=400, detail="Invalid provider")
    
    port = 8651 if provider_id == "gemini" else 8650
    # Check if already running
    info = await _get_companion_info()
    if info.get("status") == "online":
        return {"status": "already_running", "message": f"{provider_id.upper()} is already running on port {port}"}

    env = os.environ.copy()
    env["PROVIDER"] = provider_id
    env["API_PORT"] = str(port)
    env["HEADLESS"] = "false"
    
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.api.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        _COMPANION_PROCESS = subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"status": "started", "message": f"Started {provider_id.upper()} worker on port {port}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start {provider_id}: {e}")

@router.post("/admin/companion/{provider_id}/stop")
async def stop_companion(provider_id: str) -> dict[str, Any]:
    global _COMPANION_PROCESS
    port = 8651 if provider_id == "gemini" else 8650
    # Send reload / kill signal to companion port if reachable
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.post(f"http://127.0.0.1:{port}/admin/service/restart", json={"confirm": True})
    except Exception:
        pass
    if _COMPANION_PROCESS:
        try:
            _COMPANION_PROCESS.terminate()
            _COMPANION_PROCESS = None
        except Exception:
            pass
    return {"status": "stopped", "message": f"Stopped {provider_id.upper()} worker on port {port}"}


def _setting_value(name: str) -> Any:
    if name == "RELAY_VNC_PASSWORD":
        val = os.getenv("RELAY_VNC_PASSWORD") or os.getenv("VNC_PASSWORD")
        return val or ""
    value = getattr(Config, name, None)
    return str(value) if isinstance(value, Path) else value


def _validate_setting(name: str, raw: Any) -> str:
    spec = _SETTING_DEFS[name]
    kind = spec["type"]
    if isinstance(raw, str) and ("\n" in raw or "\r" in raw):
        raise ValueError(f"{name} must be a single-line value")
    if kind == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        text = str(raw).strip().lower()
        if text not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError(f"{name} must be a boolean")
        return "true" if text in {"true", "1", "yes", "on"} else "false"
    if kind == "int":
        value = int(raw)
        if value < int(spec["min"]) or value > int(spec["max"]):
            raise ValueError(f"{name} must be between {spec['min']} and {spec['max']}")
        return str(value)
    if kind == "choice":
        value = str(raw).strip().lower()
        if value not in spec["choices"]:
            raise ValueError(f"{name} must be one of: {', '.join(spec['choices'])}")
        return value
    return str(raw).strip()


def _write_env(updates: dict[str, str]) -> None:
    lines = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining and output and output[-1] != "":
        output.append("")
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _queue_snapshot() -> dict[str, Any]:
    pool = get_tab_pool()
    if pool is None:
        return {"enabled": False, "active": 0, "waiting": 0, "capacity": int(Config.MAX_CONCURRENT_REQUESTS), "persistent_sessions": [], "ephemeral_idle": 0}
    semaphore = pool._semaphore
    waiters = getattr(semaphore, "_waiters", None) or ()
    active = max(0, int(Config.MAX_CONCURRENT_REQUESTS) - int(getattr(semaphore, "_value", 0)))
    waiting = sum(1 for waiter in waiters if not waiter.done())
    now = time.monotonic()
    sessions = []
    for key in sorted(pool._pages):
        page = pool._pages.get(key)
        sessions.append({"session_key": key, "locked": bool(pool._locks.get(key) and pool._locks[key].locked()), "open": bool(pool._page_is_open(page)), "url": pool._urls.get(key, ""), "idle_seconds": round(max(0.0, now - pool._lru.get(key, now)), 1)})
    return {"enabled": True, "active": active, "waiting": waiting, "capacity": int(Config.MAX_CONCURRENT_REQUESTS), "persistent_sessions": sessions, "ephemeral_idle": int(pool._ephemeral.qsize())}


def _conversation_rows(limit: int = 200) -> list[dict[str, Any]]:
    db_path = Path(Config.API_CONVERSATION_DB)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM conversation_routes ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _conversation_counts() -> dict[str, int]:
    db_path = Path(Config.API_CONVERSATION_DB)
    if not db_path.exists():
        return {"sessions": 0, "responses": 0}
    with sqlite3.connect(db_path) as connection:
        sessions = int(connection.execute("SELECT COUNT(*) FROM conversation_routes").fetchone()[0])
        responses = int(connection.execute("SELECT COUNT(*) FROM response_routes").fetchone()[0])
    return {"sessions": sessions, "responses": responses}


def _tail_file(path: Path, max_lines: int) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=max_lines))


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/queue", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/sessions", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/logs", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/companions", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/settings", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    vue_index = _PROJECT_ROOT / "dashboard" / "dist" / "index.html"
    if vue_index.exists():
        return HTMLResponse(vue_index.read_text(encoding="utf-8"))
    if not _DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail="Dashboard asset is missing")
    return HTMLResponse(_DASHBOARD_PATH.read_text(encoding="utf-8"))


@router.get("/admin/overview")
async def admin_overview(request: Request) -> dict[str, Any]:
    queue = _queue_snapshot()
    counts = _conversation_counts()
    browser = getattr(request.app.state, "browser", None)
    client = getattr(request.app.state, "client", None)
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = int(rss_kb * (1024 if sys.platform != "darwin" else 1))
    
    current_port = int(os.getenv("API_PORT", 8650 if Config.PROVIDER == "chatgpt" else 8651))
    current_models = ["gpt-4o", "catgpt-browser"] if Config.PROVIDER == "chatgpt" else ["gemini-browser"]
    current_info = {
        "id": Config.PROVIDER,
        "name": Config.provider_name(),
        "port": current_port,
        "status": "online",
        "models": current_models,
        "memory_bytes": rss_bytes,
        "active": queue.get("active", 0),
        "capacity": queue.get("capacity", 3),
        "sessions": counts.get("sessions", 0),
    }
    is_shallow = request.query_params.get("shallow") == "true" or request.headers.get("x-relay-shallow") == "true"
    if is_shallow:
        providers = [current_info]
    else:
        companion_info = await _get_companion_info()
        providers = [current_info, companion_info] if Config.PROVIDER == "chatgpt" else [companion_info, current_info]

    return {
        "service": {"status": "online", "provider": Config.PROVIDER, "provider_name": Config.provider_name(), "uptime_seconds": int(time.time() - _STARTED_AT), "pid": os.getpid(), "memory_bytes": rss_bytes, "browser_ready": browser is not None, "client_ready": client is not None},
        "providers": providers,
        "queue": queue,
        "conversations": counts,
        "vnc": _probe_vnc_status(),
        "metrics": metrics.snapshot()
    }


@router.get("/admin/queue")
async def admin_queue() -> dict[str, Any]:
    return _queue_snapshot()


@router.post("/admin/queue/clear-idle")
async def clear_idle_queue() -> dict[str, Any]:
    pool = get_tab_pool()
    if pool is None:
        return {"closed": 0, "queue": _queue_snapshot()}
    closed = 0
    while await pool._evict_idle_persistent():
        closed += 1
    while await pool._discard_idle_ephemeral():
        closed += 1
    return {"closed": closed, "queue": _queue_snapshot()}


@router.get("/admin/sessions")
async def admin_sessions(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    return {"items": _conversation_rows(limit), "limit": limit}


@router.delete("/admin/sessions/{project_key}/{app_key}/{conversation_key}")
async def delete_session(project_key: str, app_key: str, conversation_key: str) -> dict[str, Any]:
    store = ConversationStore(Config.API_CONVERSATION_DB)
    existing = store.get_route(project_key, app_key, conversation_key)
    if existing is None:
        raise HTTPException(status_code=404, detail="Session route not found")
    store.delete_route(project_key, app_key, conversation_key)
    return {"deleted": True}


@router.get("/admin/settings")
async def admin_settings() -> dict[str, Any]:
    return {
        "items": [
            {
                "name": name,
                "value": _setting_value(name),
                "type": spec["type"],
                "choices": spec.get("choices"),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "category": spec.get("category", "General"),
                "restart_required": bool(spec["restart"]),
                "description": spec["description"],
            }
            for name, spec in _SETTING_DEFS.items()
        ],
        "env_path": str(_ENV_PATH),
    }


@router.put("/admin/settings")
async def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    unknown = sorted(set(update.values) - set(_SETTING_DEFS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported settings: {', '.join(unknown)}")
    validated: dict[str, str] = {}
    try:
        for name, value in update.values.items():
            validated[name] = _validate_setting(name, value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _write_env(validated)
    return {"saved": sorted(validated), "restart_required": any(_SETTING_DEFS[name]["restart"] for name in validated)}


@router.get("/admin/logs")
async def admin_logs(lines: int = Query(default=200, ge=20, le=2000)) -> dict[str, Any]:
    if not _LOG_DIR.exists():
        return {"file": None, "lines": []}
    candidates = [path for path in _LOG_DIR.glob("*.log") if path.is_file()]
    if not candidates:
        return {"file": None, "lines": []}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    return {"file": latest.name, "lines": [line.rstrip("\n") for line in _tail_file(latest, lines)], "updated_at": latest.stat().st_mtime}


async def _restart_process() -> None:
    await asyncio.sleep(0.35)
    for name in _SETTING_DEFS:
        os.environ.pop(name, None)
    os.execv(sys.executable, [sys.executable, *sys.argv])


@router.post("/admin/service/restart")
async def restart_service(action: ServiceAction) -> dict[str, Any]:
    if not action.confirm:
        raise HTTPException(status_code=400, detail="Restart confirmation is required")
    asyncio.create_task(_restart_process())
    return {"accepted": True, "message": "Relay restart scheduled"}


@router.post("/admin/service/reload")
async def reload_service(action: ServiceAction) -> dict[str, Any]:
    if not action.confirm:
        raise HTTPException(status_code=400, detail="Reload confirmation is required")
    try:
        os.kill(os.getpid(), signal.SIGHUP)
    except (AttributeError, OSError):
        return {"accepted": False, "message": "This process does not support SIGHUP reload; use restart instead."}
    return {"accepted": True, "message": "Reload signal sent"}
