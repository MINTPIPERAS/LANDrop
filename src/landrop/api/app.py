"""Loopback-only HTTP API for Python transfer tasks."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from landrop import credentials, hostkeys
from landrop.config import AppConfig, load_config, save_config
from landrop.transfer import TransferCancelled, TransferError, TransferResult, upload_file

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

# Origins allowed to call the API: the Tauri dev server and packaged webview.
ALLOWED_ORIGINS = {
    "http://127.0.0.1:1424",
    "http://localhost:1424",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    port: int
    token: str

    @classmethod
    def from_environment(cls) -> ServiceConfig:
        try:
            port = int(os.environ["LANDROP_SERVICE_PORT"])
            token = os.environ["LANDROP_SERVICE_TOKEN"]
        except (KeyError, ValueError) as exc:
            raise RuntimeError("本机服务启动参数缺失或格式无效") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("本机服务端口超出范围")
        if len(token) < 32:
            raise RuntimeError("本机会话令牌强度不足")
        return cls(port=port, token=token)


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    remote_dir: str = Field(min_length=1, max_length=2048)
    local_path: str = Field(min_length=1, max_length=32767)
    auth_method: Literal["key", "password"] = "key"
    password: SecretStr | None = None
    private_key_path: str | None = Field(default=None, max_length=32767)
    overwrite: bool = False
    remember_password: bool = False

    @field_validator("host", "username", "remote_dir", "local_path")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("字段中不能包含控制字符")
        return value.strip()

    @field_validator("password")
    @classmethod
    def reject_empty_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("密码不能为空")
        return value


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr | None = None


class HostKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)


class TrustRequest(HostKeyRequest):
    fingerprint: str = Field(min_length=1, max_length=255)


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: SecretStr


@dataclass(slots=True)
class TransferTask:
    task_id: str
    request: TransferRequest
    password: str | None = field(default=None, repr=False)
    status: str = "queued"
    phase: str = "queued"
    sent: int = 0
    total: int = 0
    speed: float = 0.0
    message: str = "等待传输"
    error: str | None = None
    result: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=256))
    next_event_id: int = 1
    condition: threading.Condition = field(default_factory=threading.Condition)
    last_progress_time: float = field(default_factory=time.monotonic)
    last_progress_sent: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "status": self.status,
            "phase": self.phase,
            "sent": self.sent,
            "total": self.total,
            "speed": round(self.speed, 1),
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "request": {
                "host": self.request.host,
                "port": self.request.port,
                "username": self.request.username,
                "remote_dir": self.request.remote_dir,
                "local_path": self.request.local_path,
                "auth_method": self.request.auth_method,
                "private_key_path": self.request.private_key_path,
                "overwrite": self.request.overwrite,
            },
        }

    def publish(self, **changes: Any) -> None:
        with self.condition:
            for key, value in changes.items():
                setattr(self, key, value)
            event = {"id": self.next_event_id, "data": self.snapshot()}
            self.next_event_id += 1
            self.events.append(event)
            self.condition.notify_all()


class TransferManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._tasks: dict[str, TransferTask] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="landrop-transfer")

    def start(self, request: TransferRequest) -> TransferTask:
        local_path = Path(request.local_path).expanduser().resolve()
        if not local_path.is_file():
            raise ValueError("选择的本地文件不存在或不是普通文件")
        if request.auth_method == "key" and request.private_key_path:
            if not Path(request.private_key_path).expanduser().is_file():
                raise ValueError("指定的 SSH 私钥文件不存在")

        password = request.password.get_secret_value() if request.password else None
        if request.auth_method == "password" and password is None:
            password = credentials.load_password(request.username)
        if request.auth_method == "password" and password is None:
            raise ValueError("密码认证需要提供密码，或先保存该用户的密码")
        if password is not None and request.remember_password:
            credentials.save_password(request.username, password)

        # Keep credentials out of the retained request object.
        stored = request.model_copy(update={"password": None})
        task = TransferTask(
            task_id=uuid.uuid4().hex,
            request=stored,
            password=password,
            total=local_path.stat().st_size,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        task.publish(status="queued", phase="queued", message="等待传输")
        self._executor.submit(self._run, task)
        return task

    def retry(self, task_id: str, password: str | None = None) -> TransferTask | None:
        previous = self.get(task_id)
        if previous is None:
            return None
        with previous.condition:
            if previous.status not in TERMINAL_STATUSES:
                raise ValueError("只有已结束的任务才能重试")
            request = previous.request.model_copy(deep=True)
        if request.auth_method == "password" and password is None:
            password = credentials.load_password(request.username)
        if request.auth_method == "password" and password is None:
            raise ValueError("重试密码认证任务需要重新提供密码")
        task = TransferTask(
            task_id=uuid.uuid4().hex,
            request=request,
            password=password,
            total=Path(request.local_path).expanduser().stat().st_size,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        task.publish(status="queued", phase="queued", message="等待重试")
        self._executor.submit(self._run, task)
        return task

    def get(self, task_id: str) -> TransferTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> TransferTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        with task.condition:
            if task.status not in TERMINAL_STATUSES:
                task.cancel_event.set()
        return task

    def close(self) -> None:
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            self.cancel(task.task_id)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, task: TransferTask) -> None:
        request = task.request
        task.publish(status="running", phase="connecting", message="正在连接远端")

        def report_progress(sent: int, total: int) -> None:
            now = time.monotonic()
            elapsed = now - task.last_progress_time
            speed = (sent - task.last_progress_sent) / elapsed if elapsed > 0 else 0.0
            task.last_progress_time = now
            task.last_progress_sent = sent
            task.publish(status="running", phase="transferring", sent=sent, total=total,
                         speed=max(0.0, speed), message="正在传输")

        try:
            task.publish(phase="checking", message="检查续传状态")
            trusted_hosts = hostkeys.known_hosts_path()
            result = upload_file(
                host=request.host, port=request.port, username=request.username,
                local_path=request.local_path, remote_dir=request.remote_dir,
                auth_method=request.auth_method,
                password=task.password,
                private_key_path=request.private_key_path, progress=report_progress,
                cancel=task.cancel_event, overwrite=request.overwrite,
                known_hosts_path=trusted_hosts if trusted_hosts.is_file() else None,
            )
            task.publish(phase="verifying", message="校验文件")
            task.publish(status="succeeded", phase="completed", sent=result.size, total=result.size,
                         speed=0.0, message="传输完成", result=_serialize_result(result))
        except TransferCancelled as exc:
            task.publish(status="cancelled", phase="cancelled", message=str(exc), speed=0.0)
        except (TransferError, ValueError) as exc:
            task.publish(status="failed", phase="failed", message="传输失败", error=str(exc), speed=0.0)
        except Exception as exc:
            logger.exception("Transfer task failed")
            task.publish(status="failed", phase="failed", message="传输失败，请查看本机日志",
                         error=type(exc).__name__, speed=0.0)
        finally:
            # Credentials live only for the duration of the task.
            task.password = None


def _serialize_result(result: TransferResult) -> dict[str, Any]:
    return {"remote_path": result.remote_path, "size": result.size,
            "sha256": result.sha256, "resumed_from": result.resumed_from}


def _wait_for_events(task: TransferTask, cursor: int) -> tuple[list[dict[str, Any]], bool]:
    with task.condition:
        events = [event for event in task.events if event["id"] > cursor]
        terminal = task.status in TERMINAL_STATUSES
        if not events and not terminal:
            task.condition.wait(timeout=10)
            events = [event for event in task.events if event["id"] > cursor]
            terminal = task.status in TERMINAL_STATUSES
        return events, terminal


def create_app(token: str | None = None, manager: TransferManager | None = None) -> FastAPI:
    session_token = token if token is not None else secrets.token_urlsafe(32)
    if len(session_token) < 32:
        raise ValueError("本机会话令牌至少需要 32 个字符")
    transfer_manager = manager or TransferManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if manager is None:
            await asyncio.to_thread(transfer_manager.close)

    app = FastAPI(title="LANDrop local service", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.session_token = session_token
    app.state.transfer_manager = transfer_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-LANDrop-Token", "Authorization", "Last-Event-ID"],
        expose_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def enforce_local_caller(request: Request, call_next):
        client = request.client
        if client is None:
            return JSONResponse(status_code=403, content={"detail": "仅允许本机访问"})
        try:
            address = ipaddress.ip_address(client.host)
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "仅允许 IP loopback 客户端访问"})
        if not address.is_loopback:
            return JSONResponse(status_code=403, content={"detail": "仅允许本机访问"})

        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "请求来源不被允许"})
        return await call_next(request)

    async def require_token(
        authorization: Annotated[str | None, Header()] = None,
        x_landrop_token: Annotated[str | None, Header(alias="X-LANDrop-Token")] = None,
    ) -> None:
        bearer = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else None
        supplied = x_landrop_token or bearer
        if not supplied or not hmac.compare_digest(supplied, app.state.session_token):
            raise HTTPException(status_code=401, detail="本机会话令牌无效",
                                headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(require_token)]

    @app.get("/health", dependencies=protected)
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/api/config", dependencies=protected)
    async def read_config() -> dict[str, Any]:
        try:
            config = load_config()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _serialize_config(config)

    @app.put("/api/config", dependencies=protected)
    async def write_config(payload: AppConfigPayload) -> dict[str, Any]:
        try:
            config = AppConfig(**payload.model_dump()).normalized()
            save_config(config)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _serialize_config(config)

    @app.post("/api/hostkeys/probe", dependencies=protected)
    async def probe_host_key(payload: HostKeyRequest) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(hostkeys.probe, payload.host, payload.port)
        except (hostkeys.HostKeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"host": info.host, "port": info.port, "key_type": info.key_type,
                "fingerprint": info.fingerprint, "trusted": info.trusted}

    @app.post("/api/hostkeys/trust", dependencies=protected)
    async def trust_host_key(payload: TrustRequest) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(
                hostkeys.trust, payload.host, payload.port, payload.fingerprint
            )
        except (hostkeys.HostKeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"host": info.host, "port": info.port, "key_type": info.key_type,
                "fingerprint": info.fingerprint, "trusted": info.trusted}

    @app.put("/api/credentials", dependencies=protected)
    async def save_credential(payload: CredentialRequest) -> dict[str, bool]:
        try:
            await asyncio.to_thread(
                credentials.save_password, payload.username, payload.password.get_secret_value()
            )
        except (credentials.CredentialError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": True}

    @app.delete("/api/credentials/{username}", dependencies=protected)
    async def delete_credential(username: str) -> dict[str, bool]:
        try:
            removed = await asyncio.to_thread(credentials.delete_password, username)
        except credentials.CredentialError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": removed}

    @app.get("/api/credentials/{username}", dependencies=protected)
    async def credential_status(username: str) -> dict[str, bool]:
        exists = await asyncio.to_thread(credentials.has_password, username)
        return {"saved": exists}

    @app.post("/api/transfers", status_code=202, dependencies=protected)
    async def create_transfer(payload: TransferRequest) -> dict[str, Any]:
        try:
            task = await asyncio.to_thread(transfer_manager.start, payload)
        except (ValueError, credentials.CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return task.snapshot()

    @app.get("/api/transfers/{task_id}", dependencies=protected)
    async def get_transfer(task_id: str) -> dict[str, Any]:
        task = transfer_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="找不到该传输任务")
        return task.snapshot()

    @app.post("/api/transfers/{task_id}/retry", status_code=202, dependencies=protected)
    async def retry_transfer(task_id: str, payload: RetryRequest | None = None) -> dict[str, Any]:
        password = payload.password.get_secret_value() if payload and payload.password else None
        try:
            task = await asyncio.to_thread(transfer_manager.retry, task_id, password)
        except (ValueError, credentials.CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="找不到该传输任务")
        return task.snapshot()

    @app.post("/api/transfers/{task_id}/cancel", dependencies=protected)
    async def cancel_transfer(task_id: str) -> dict[str, Any]:
        task = transfer_manager.cancel(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="找不到该传输任务")
        return task.snapshot()

    @app.get("/api/transfers/{task_id}/events", dependencies=protected)
    async def transfer_events(task_id: str, request: Request,
                              last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
        task = transfer_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="找不到该传输任务")
        try:
            cursor = int(last_event_id or "0")
            if cursor < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID 格式无效") from exc

        async def stream_events():
            nonlocal cursor
            while not await request.is_disconnected():
                events, terminal = await asyncio.to_thread(_wait_for_events, task, cursor)
                if not events and not terminal:
                    yield ": keep-alive\n\n"
                    continue
                for event in events:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                if terminal and (not events or events[-1]["data"]["status"] in TERMINAL_STATUSES):
                    break

        return StreamingResponse(stream_events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


class AppConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="", max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="", max_length=255)
    remote_dir: str = Field(default="", max_length=2048)
    auth_method: Literal["password", "key"] = "key"
    private_key_path: str = Field(default="", max_length=32767)
    hotkey_modifiers: list[str] = Field(default_factory=lambda: ["ctrl", "alt"])
    hotkey_key: str = Field(default="space", max_length=32)


def _serialize_config(config: AppConfig) -> dict[str, Any]:
    return {
        "host": config.host,
        "port": config.port,
        "username": config.username,
        "remote_dir": config.remote_dir,
        "auth_method": config.auth_method,
        "private_key_path": config.private_key_path,
        "hotkey_modifiers": list(config.hotkey_modifiers),
        "hotkey_key": config.hotkey_key,
    }


def _log_file() -> Path | None:
    """Where the packaged sidecar records failures, since it has no console."""
    if os.name != "nt":
        return None
    root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return root / "LANDrop" / "service.log"


def _configure_logging() -> None:
    target = _log_file()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(handler)


def run() -> None:
    import uvicorn

    _configure_logging()
    try:
        config = ServiceConfig.from_environment()
        uvicorn.run(create_app(config.token), host="127.0.0.1", port=config.port,
                    access_log=False, log_level="warning")
    except SystemExit:
        raise
    except BaseException:
        # The packaged sidecar has no console, so an unlogged crash would look
        # like "the app never connected" with nothing to go on.
        logger.exception("本机传输服务异常退出")
        raise


if __name__ == "__main__":
    run()
