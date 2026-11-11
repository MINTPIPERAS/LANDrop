"""Loopback-only HTTP API for Python transfer tasks."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from landrop import credentials, hostkeys, logs, scheduler
from landrop.config import AppConfig, load_config, parse_schedule_time, save_config
from landrop.transfer import (
    TransferCancelled,
    TransferError,
    TransferResult,
    TreeResult,
    scan_directory,
    upload_file,
    upload_tree,
)

# Fixed name: ``python -m landrop.api.app`` would otherwise report __main__ and
# inherit the root WARNING level, silently dropping every INFO record in dev.
logger = logging.getLogger("landrop.api.app")

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "partial"}

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
    recursive: bool = False
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


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=32767)


class TrustRequest(HostKeyRequest):
    fingerprint: str = Field(min_length=1, max_length=255)


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
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
    error_code: str | None = None
    current_file: str | None = None
    files_done: int = 0
    files_total: int = 0
    verified: int = 0
    verify_total: int = 0
    result: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=256))
    next_event_id: int = 1
    condition: threading.Condition = field(default_factory=threading.Condition)
    last_progress_time: float = field(default_factory=time.monotonic)
    last_progress_sent: int = 0
    created_at: float = field(default_factory=time.time)
    source: str = "manual"

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
            "error_code": self.error_code,
            "current_file": self.current_file,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "verified": self.verified,
            "verify_total": self.verify_total,
            "created_at": self.created_at,
            "source": self.source,
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
                "recursive": self.request.recursive,
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

    def start(self, request: TransferRequest, *, source: str = "manual") -> TransferTask:
        local_path = Path(request.local_path).expanduser().resolve()
        if request.recursive:
            if not local_path.is_dir():
                raise ValueError("选择的本地文件夹不存在或不是目录")
        elif not local_path.is_file():
            raise ValueError("选择的本地文件不存在或不是普通文件")
        total = _source_total(request)
        if request.auth_method == "key" and request.private_key_path:
            if not Path(request.private_key_path).expanduser().is_file():
                raise ValueError("指定的 SSH 私钥文件不存在")

        password = request.password.get_secret_value() if request.password else None
        if request.auth_method == "password" and password is None:
            password = credentials.load_password(request.host, request.port, request.username)
        if request.auth_method == "password" and password is None:
            raise ValueError("密码认证需要提供密码，或先保存该用户的密码")
        if password is not None and request.remember_password:
            credentials.save_password(request.host, request.port, request.username, password)

        # Keep credentials out of the retained request object.
        stored = request.model_copy(update={"password": None})
        task = TransferTask(
            task_id=uuid.uuid4().hex,
            request=stored,
            password=password,
            total=total,
            source=source,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        task.publish(status="queued", phase="queued", message="等待传输")
        self._executor.submit(self._run, task)
        logger.info(
            "任务入队 %s：%s → %s@%s:%s，目标 %s%s",
            task.task_id,
            local_path,
            request.username,
            request.host,
            request.port,
            request.remote_dir,
            "（文件夹）" if request.recursive else "",
        )
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
            password = credentials.load_password(request.host, request.port, request.username)
        if request.auth_method == "password" and password is None:
            raise ValueError("重试密码认证任务需要重新提供密码")
        task = TransferTask(
            task_id=uuid.uuid4().hex,
            request=request,
            password=password,
            total=_source_total(request),
        )
        with self._lock:
            self._tasks[task.task_id] = task
        task.publish(status="queued", phase="queued", message="等待重试")
        self._executor.submit(self._run, task)
        return task

    def get(self, task_id: str) -> TransferTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> list[TransferTask]:
        """Newest first; the UI needs this to see scheduler-dispatched tasks."""
        with self._lock:
            tasks = sorted(
                self._tasks.values(), key=lambda item: item.created_at, reverse=True
            )
        return tasks[: max(0, limit)]

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
        started = time.monotonic()
        task.publish(status="running", phase="connecting", message="正在连接远端")

        def report_progress(sent: int, total: int) -> None:
            now = time.monotonic()
            elapsed = now - task.last_progress_time
            speed = (sent - task.last_progress_sent) / elapsed if elapsed > 0 else 0.0
            task.last_progress_time = now
            task.last_progress_sent = sent
            task.publish(status="running", phase="transferring", sent=sent, total=total,
                         speed=max(0.0, speed), message="正在传输")

        def report_file(rel_path: str, done: int, files_total: int) -> None:
            task.publish(status="running", current_file=rel_path,
                         files_done=done, files_total=files_total)

        def report_verify(done: int, total_bytes: int) -> None:
            # Verification re-reads both sides; without these events the UI sat
            # on "检查续传" for minutes on multi-GB files.
            task.publish(status="running", phase="verifying", message="校验文件",
                         verified=done, verify_total=total_bytes)

        try:
            task.publish(phase="checking", message="检查续传状态")
            trusted_hosts = hostkeys.known_hosts_path()
            known_hosts = trusted_hosts if trusted_hosts.is_file() else None
            if request.recursive:
                tree_result = upload_tree(
                    host=request.host, port=request.port, username=request.username,
                    local_path=request.local_path, remote_dir=request.remote_dir,
                    auth_method=request.auth_method,
                    password=task.password,
                    private_key_path=request.private_key_path,
                    progress=report_progress, file_progress=report_file,
                    verify_progress=report_verify,
                    cancel=task.cancel_event, overwrite=request.overwrite,
                    known_hosts_path=known_hosts,
                )
                failed = tree_result.failed_count
                task.publish(
                    status="partial" if failed else "succeeded",
                    phase="partial" if failed else "completed",
                    sent=tree_result.size, total=tree_result.size, speed=0.0,
                    message=f"传输完成，{failed} 个文件失败" if failed else "传输完成",
                    current_file=None,
                    files_done=tree_result.ok_count,
                    files_total=len(tree_result.files),
                    result=_serialize_tree_result(tree_result),
                )
            else:
                result = upload_file(
                    host=request.host, port=request.port, username=request.username,
                    local_path=request.local_path, remote_dir=request.remote_dir,
                    auth_method=request.auth_method,
                    password=task.password,
                    private_key_path=request.private_key_path, progress=report_progress,
                    verify_progress=report_verify,
                    cancel=task.cancel_event, overwrite=request.overwrite,
                    known_hosts_path=known_hosts,
                )
                task.publish(phase="verifying", message="校验文件")
                task.publish(status="succeeded", phase="completed", sent=result.size, total=result.size,
                             speed=0.0, message="传输完成", result=_serialize_result(result))
        except TransferCancelled as exc:
            task.publish(status="cancelled", phase="cancelled", message=str(exc), speed=0.0)
        except (TransferError, ValueError) as exc:
            task.publish(status="failed", phase="failed", message="传输失败", error=str(exc),
                         error_code=getattr(exc, "code", "transfer_failed"), speed=0.0)
        except Exception as exc:
            logger.exception("Transfer task failed")
            task.publish(status="failed", phase="failed", message="传输失败，请查看本机日志",
                         error=type(exc).__name__, error_code="internal_error", speed=0.0)
        finally:
            # Credentials live only for the duration of the task.
            task.password = None
            logger.info(
                "任务结束 %s：%s，%s/%s 字节，耗时 %.1fs%s",
                task.task_id,
                task.status,
                task.sent,
                task.total,
                time.monotonic() - started,
                f"，错误 {task.error_code or task.error}" if task.error else "",
            )


def _serialize_result(result: TransferResult) -> dict[str, Any]:
    return {"remote_path": result.remote_path, "size": result.size,
            "sha256": result.sha256, "resumed_from": result.resumed_from}


def _serialize_tree_result(result: TreeResult) -> dict[str, Any]:
    return {
        "remote_path": result.remote_root,
        "size": result.size,
        "sha256": None,
        "resumed_from": 0,
        "ok_count": result.ok_count,
        "failed_count": result.failed_count,
        "skipped": list(result.skipped),
        "files": [
            {
                "rel_path": item.rel_path,
                "remote_path": item.remote_path,
                "size": item.size,
                "ok": item.ok,
                "error": item.error,
                "error_code": item.error_code,
            }
            for item in result.files
        ],
    }


def _preserve_schedule_state(config: AppConfig) -> AppConfig:
    """Keep run state server-owned so a stale client cannot erase or revive it.

    The frontend holds a config snapshot from load time; without this, the next
    save (device switch, hotkey change...) would wipe ``last_run_*`` recorded by
    the scheduler and could re-enable a one-shot rule that already ran.
    """
    try:
        current = load_config()
    except ValueError:
        return config
    known = {item.id: item for item in current.schedules}
    updated = []
    for item in config.schedules:
        previous = known.get(item.id)
        if previous is None:
            updated.append(item)
            continue
        same_time = (
            item.mode == previous.mode
            and item.date == previous.date
            and item.time == previous.time
        )
        enabled = item.enabled
        if previous.mode == "once" and previous.last_run_at and same_time:
            enabled = False
        updated.append(
            replace(
                item,
                enabled=enabled,
                last_run_at=previous.last_run_at,
                last_run_status=previous.last_run_status,
            )
        )
    return replace(config, schedules=tuple(updated))


def _validate_schedules(config: AppConfig) -> None:
    """Reject schedules that could never run, before they are saved."""
    now = datetime.now()
    for schedule in config.schedules:
        label = schedule.name or schedule.id
        source = Path(schedule.source_path).expanduser()
        if not source.exists():
            raise ValueError(f"定时任务「{label}」的来源不存在：{source}")
        if not schedule.enabled:
            continue
        if schedule.mode == "once":
            try:
                hour, minute = parse_schedule_time(schedule.time)
                run_at = datetime.combine(
                    date.fromisoformat(schedule.date), clock_time(hour, minute)
                )
            except ValueError:
                pass  # date/time format is reported by ScheduleConfig.validate
            else:
                if run_at <= now:
                    raise ValueError(
                        f"定时任务「{label}」的时间已经过去，请选择未来的时间"
                    )
        device = next((item for item in config.devices if item.id == schedule.device_id), None)
        if device is None:
            raise ValueError(f"定时任务「{label}」引用的设备不存在")
        if not (schedule.remote_dir.strip() or device.remote_dir.strip()):
            raise ValueError(f"定时任务「{label}」没有可用的目标目录")
        if device.auth_method == "password":
            if not credentials.has_password(device.host, device.port, device.username):
                raise ValueError(f"定时任务「{label}」需要先为该设备保存密码")


def _source_total(request: TransferRequest) -> int:
    """Total bytes a task will move; also validates the local source exists."""
    path = Path(request.local_path).expanduser()
    if request.recursive:
        if not path.is_dir():
            raise ValueError("本地文件夹不存在或不是目录")
        return scan_directory(path).total_size
    if not path.is_file():
        raise ValueError("本地文件不存在或不是普通文件")
    return path.stat().st_size


def _wait_for_events(task: TransferTask, cursor: int) -> tuple[list[dict[str, Any]], bool]:
    with task.condition:
        events = [event for event in task.events if event["id"] > cursor]
        terminal = task.status in TERMINAL_STATUSES
        if not events and not terminal:
            task.condition.wait(timeout=10)
            events = [event for event in task.events if event["id"] > cursor]
            terminal = task.status in TERMINAL_STATUSES
        return events, terminal


def create_app(
    token: str | None = None,
    manager: TransferManager | None = None,
    *,
    enable_scheduler: bool = False,
) -> FastAPI:
    session_token = token if token is not None else secrets.token_urlsafe(32)
    if len(session_token) < 32:
        raise ValueError("本机会话令牌至少需要 32 个字符")
    transfer_manager = manager or TransferManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        loop = None
        if enable_scheduler:
            loop = scheduler.ScheduleLoop(transfer_manager)
            loop.start()
        try:
            yield
        finally:
            if loop is not None:
                loop.stop()
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
        with scheduler.config_lock():
            try:
                config = AppConfig(**payload.model_dump()).normalized()
                config.validate()
                logs.validate_log_dir(config.log_dir)
                config = _preserve_schedule_state(config)
                _validate_schedules(config)
                save_config(config)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        directory = logs.configure_logging(config.log_dir)
        if directory is not None:
            removed = logs.purge(directory, config.log_retention_days)
            if removed:
                logger.info("日志清理：删除 %s 个过期文件", len(removed))
        try:
            await asyncio.to_thread(scheduler.sync_tasks, config.schedules)
        except Exception:  # noqa: BLE001 - task registration must not block saving
            logger.exception("同步系统计划任务失败")
        logger.info(
            "配置已保存：%s 台设备，活动设备 %s，%s 个定时任务，日志保留 %s 天",
            len(config.devices),
            config.active_device_id or "无",
            len(config.schedules),
            config.log_retention_days,
        )
        return _serialize_config(config)

    @app.get("/api/schedules", dependencies=protected)
    async def schedule_status() -> dict[str, Any]:
        try:
            config = load_config()
        except ValueError:
            config = AppConfig()
        now = datetime.now()
        items = []
        for schedule in config.schedules:
            upcoming = scheduler.next_run_at(schedule, now)
            items.append(
                {
                    "id": schedule.id,
                    "next_run_at": upcoming.isoformat(timespec="seconds") if upcoming else None,
                    "missed": schedule.enabled and scheduler.is_missed(schedule, now),
                    "last_run_at": schedule.last_run_at,
                    "last_run_status": schedule.last_run_status,
                    "enabled": schedule.enabled,
                }
            )
        return {
            "schedules": items,
            "system_tasks": scheduler.system_tasks_supported(),
        }

    @app.post("/api/schedules/{schedule_id}/run", status_code=202, dependencies=protected)
    async def run_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            config = load_config()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        schedule = next((item for item in config.schedules if item.id == schedule_id), None)
        if schedule is None:
            raise HTTPException(status_code=404, detail="找不到该定时任务")
        if not Path(schedule.source_path).expanduser().exists():
            raise HTTPException(status_code=400, detail="定时任务的来源路径不存在")
        try:
            task = await asyncio.to_thread(
                scheduler.dispatch_schedule, transfer_manager, config, schedule
            )
        except (ValueError, credentials.CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return task.snapshot()

    @app.get("/api/logs", dependencies=protected)
    async def read_logs() -> dict[str, Any]:
        try:
            config = load_config()
        except ValueError:
            config = AppConfig()
        directory = logs.resolve_log_dir(config.log_dir)
        return await asyncio.to_thread(logs.describe, directory)

    @app.post("/api/logs/open", dependencies=protected)
    async def open_log_dir() -> dict[str, bool]:
        try:
            config = load_config()
        except ValueError:
            config = AppConfig()
        directory = logs.resolve_log_dir(config.log_dir)
        opened = await asyncio.to_thread(logs.open_directory, directory)
        return {"opened": opened}

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
                credentials.save_password,
                payload.host,
                payload.port,
                payload.username,
                payload.password.get_secret_value(),
            )
        except (credentials.CredentialError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": True}

    @app.delete("/api/credentials", dependencies=protected)
    async def delete_credential(host: str, username: str, port: int = 22) -> dict[str, bool]:
        try:
            removed = await asyncio.to_thread(
                credentials.delete_password, host, port, username
            )
        except (credentials.CredentialError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": removed}

    @app.get("/api/credentials", dependencies=protected)
    async def credential_status(host: str, username: str, port: int = 22) -> dict[str, bool]:
        try:
            exists = await asyncio.to_thread(
                credentials.has_password, host, port, username
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": exists}

    @app.post("/api/scan", dependencies=protected)
    async def scan_folder(payload: ScanRequest) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(scan_directory, payload.path)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "root": str(result.root),
            "total_files": result.total_files,
            "total_size": result.total_size,
            "empty_dirs": len(result.empty_dirs),
            "skipped": list(result.skipped),
        }

    @app.post("/api/transfers", status_code=202, dependencies=protected)
    async def create_transfer(payload: TransferRequest) -> dict[str, Any]:
        try:
            task = await asyncio.to_thread(transfer_manager.start, payload)
        except (ValueError, credentials.CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return task.snapshot()

    @app.get("/api/transfers", dependencies=protected)
    async def list_transfers(limit: int = 50) -> dict[str, Any]:
        bounded = min(max(limit, 1), 200)
        return {"tasks": [task.snapshot() for task in transfer_manager.list_tasks(bounded)]}

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


class DevicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=255)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="", max_length=255)
    remote_dir: str = Field(default="", max_length=2048)
    auth_method: Literal["password", "key"] = "key"
    private_key_path: str = Field(default="", max_length=32767)


class SchedulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=255)
    enabled: bool = True
    source_path: str = Field(default="", max_length=32767)
    recursive: bool = False
    device_id: str = Field(default="", max_length=64)
    remote_dir: str = Field(default="", max_length=2048)
    overwrite: bool = False
    mode: Literal["once", "daily", "weekly"] = "once"
    time: str = Field(default="09:00", max_length=5)
    date: str = Field(default="", max_length=10)
    weekdays: list[int] = Field(default_factory=list)
    last_run_at: str = Field(default="", max_length=32)
    last_run_status: str = Field(default="", max_length=16)


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
    devices: list[DevicePayload] = Field(default_factory=list)
    active_device_id: str = Field(default="", max_length=64)
    log_dir: str = Field(default="", max_length=32767)
    log_retention_days: int = Field(default=14, ge=0, le=3650)
    schedules: list[SchedulePayload] = Field(default_factory=list)
    close_to_tray: bool = True


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
        "devices": [
            {
                "id": device.id,
                "name": device.name,
                "host": device.host,
                "port": device.port,
                "username": device.username,
                "remote_dir": device.remote_dir,
                "auth_method": device.auth_method,
                "private_key_path": device.private_key_path,
            }
            for device in config.devices
        ],
        "active_device_id": config.active_device_id,
        "log_dir": config.log_dir,
        "log_retention_days": config.log_retention_days,
        "schedules": [
            {
                "id": schedule.id,
                "name": schedule.name,
                "enabled": schedule.enabled,
                "source_path": schedule.source_path,
                "recursive": schedule.recursive,
                "device_id": schedule.device_id,
                "remote_dir": schedule.remote_dir,
                "overwrite": schedule.overwrite,
                "mode": schedule.mode,
                "time": schedule.time,
                "date": schedule.date,
                "weekdays": list(schedule.weekdays),
                "last_run_at": schedule.last_run_at,
                "last_run_status": schedule.last_run_status,
            }
            for schedule in config.schedules
        ],
        "close_to_tray": config.close_to_tray,
    }


def _bootstrap_logging() -> Path | None:
    """Install file logging from the saved config, degrading quietly on failure."""
    try:
        app_config = load_config()
    except ValueError:
        app_config = AppConfig()
    directory = logs.configure_logging(app_config.log_dir)
    if directory is None:
        return None
    logs.migrate_legacy_log(directory)
    removed = logs.purge(directory, app_config.log_retention_days)
    logger.info(
        "日志已启用：%s（保留 %s 天，清理 %s 个过期文件）",
        directory,
        app_config.log_retention_days,
        len(removed),
    )
    return directory


def _schedule_argument(argv: list[str]) -> str | None:
    """Return the schedule id passed by Task Scheduler, if any."""
    if "--run-schedule" not in argv:
        return None
    index = argv.index("--run-schedule")
    if index + 1 >= len(argv):
        return ""
    return argv[index + 1]


def run() -> None:
    import uvicorn

    _bootstrap_logging()
    schedule_id = _schedule_argument(sys.argv[1:])
    lock = scheduler.RunLock()
    acquired = lock.acquire()
    if not acquired and schedule_id is not None:
        logger.info("应用正在运行，跳过本次定时任务 %s", schedule_id)
        raise SystemExit(0)
    if not acquired:
        logger.warning("另一个 LANDrop 实例正在运行，本实例仍会启动")
    try:
        if schedule_id is not None:
            raise SystemExit(scheduler.run_once(schedule_id))
        config = ServiceConfig.from_environment()
        logger.info("本机传输服务启动：127.0.0.1:%s", config.port)
        uvicorn.run(create_app(config.token, enable_scheduler=True), host="127.0.0.1",
                    port=config.port, access_log=False, log_level="warning")
    except SystemExit:
        raise
    except BaseException:
        # The packaged sidecar has no console, so an unlogged crash would look
        # like "the app never connected" with nothing to go on.
        logger.exception("本机传输服务异常退出")
        raise
    finally:
        lock.release()
        logger.info("本机传输服务已退出")


if __name__ == "__main__":
    run()
