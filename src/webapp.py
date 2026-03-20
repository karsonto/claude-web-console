from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SESSION_STATE_FILE = DATA_DIR / "sessions.json"

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Skill",
    "Task",
    "TaskOutput",
    "NotebookEdit",
    "AskUserQuestion",
    "ToolSearch",
]

PROMPT_APPEND = (
    "You are responding in a browser-based Claude Code chat. "
    "Prefer using available skills when they meaningfully improve task completion. "
    "Keep user-facing progress updates concise and readable."
)


class CreateSessionResponse(BaseModel):
    session_id: str
    model: str | None = None
    permission_mode: str | None = None
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class MessageRecord(BaseModel):
    role: str
    content: str
    meta: list[dict[str, str]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class SessionStateResponse(CreateSessionResponse):
    history: list[MessageRecord] = Field(default_factory=list)
    active: bool = True


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1)


@dataclass
class ChatSession:
    session_id: str
    client: ClaudeSDKClient
    created_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    server_info: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    claude_session_id: str | None = None


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._lock = asyncio.Lock()
        DATA_DIR.mkdir(exist_ok=True)

    def _read_storage(self) -> dict[str, Any]:
        if not SESSION_STATE_FILE.exists():
            return {}
        try:
            return json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_storage(self, payload: dict[str, Any]) -> None:
        SESSION_STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _persist_sessions(self) -> None:
        async with self._lock:
            payload = {
                session_id: {
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "server_info": session.server_info,
                    "history": session.history,
                    "claude_session_id": session.claude_session_id,
                    "permission_mode": session.client.options.permission_mode,
                    "active": True,
                }
                for session_id, session in self._sessions.items()
            }
        self._write_storage(payload)

    async def append_history(
        self,
        session: ChatSession,
        role: str,
        content: str,
        meta: list[dict[str, str]] | None = None,
    ) -> None:
        session.history.append(
            MessageRecord(
                role=role,
                content=content,
                meta=meta or [],
            ).model_dump()
        )
        await self._persist_sessions()

    async def _safe_disconnect(self, session: ChatSession) -> None:
        try:
            await session.client.disconnect()
        except RuntimeError:
            pass

    async def create_session(self) -> ChatSession:
        session_id = uuid.uuid4().hex
        permission_mode = os.getenv("CLAUDE_WEB_PERMISSION_MODE", "acceptEdits")

        options = ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            allowed_tools=DEFAULT_ALLOWED_TOOLS,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": PROMPT_APPEND,
            },
            permission_mode=permission_mode,
            setting_sources=["user", "project", "local"],
            include_partial_messages=True,
            cwd=BASE_DIR,
        )

        client = ClaudeSDKClient(options=options)
        await client.connect()
        info = await client.get_server_info()

        session = ChatSession(
            session_id=session_id,
            client=client,
            server_info=info,
        )
        async with self._lock:
            self._sessions[session_id] = session
        await self._persist_sessions()
        return session

    async def get_session(self, session_id: str) -> ChatSession:
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            await self._safe_disconnect(session)
        storage = self._read_storage()
        storage.pop(session_id, None)
        self._write_storage(storage)

    async def interrupt_session(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        await session.client.interrupt()

    async def get_persisted_state(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            info = session_response(session).model_dump()
            return {
                **info,
                "history": session.history,
                "active": True,
            }

        storage = self._read_storage()
        stored = storage.get(session_id)
        if stored is None:
            return None

        server_info = stored.get("server_info") or {}
        models = server_info.get("models", [])
        commands = server_info.get("commands", [])
        agents = server_info.get("agents", [])
        return {
            "session_id": session_id,
            "model": models[0].get("displayName") if models else None,
            "permission_mode": stored.get("permission_mode"),
            "skills": [
                command["name"]
                for command in commands
                if isinstance(command, dict) and command.get("name")
            ],
            "tools": [
                agent["name"]
                for agent in agents
                if isinstance(agent, dict) and agent.get("name")
            ],
            "history": stored.get("history", []),
            "active": False,
        }

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            await self._safe_disconnect(session)
        await self._persist_sessions()


session_manager = SessionManager()


def json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def compact_text(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def session_response(session: ChatSession) -> CreateSessionResponse:
    info = session.server_info or {}
    models = info.get("models", [])
    commands = info.get("commands", [])
    agents = info.get("agents", [])
    return CreateSessionResponse(
        session_id=session.session_id,
        model=models[0].get("displayName") if models else None,
        permission_mode=session.client.options.permission_mode,
        skills=[
            command["name"]
            for command in commands
            if isinstance(command, dict) and command.get("name")
        ],
        tools=[
            agent["name"]
            for agent in agents
            if isinstance(agent, dict) and agent.get("name")
        ],
    )


def meta_entry(text: str, variant: str = "info") -> dict[str, str]:
    return {"text": text, "variant": variant}


def stream_event_to_payloads(event: dict[str, Any]) -> list[dict[str, Any]]:
    if event.get("type") != "content_block_delta":
        return []

    delta = event.get("delta", {})
    if delta.get("type") == "text_delta":
        text = delta.get("text", "")
        if text:
            return [{"type": "text_delta", "text": text}]
    return []


async def stream_chat_response(
    session: ChatSession, prompt: str
) -> AsyncIterator[bytes]:
    saw_text_delta = False
    assistant_text_parts: list[str] = []
    assistant_meta: list[dict[str, str]] = []

    async with session.lock:
        try:
            await session_manager.append_history(session, "user", prompt)
            yield json_line({"type": "status", "message": "Claude 正在思考..."})
            await session.client.query(prompt, session_id=session.session_id)

            async for message in session.client.receive_response():
                if isinstance(message, StreamEvent):
                    for payload in stream_event_to_payloads(message.event):
                        if payload["type"] == "text_delta":
                            saw_text_delta = True
                            assistant_text_parts.append(payload["text"])
                        yield json_line(payload)
                    continue

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            if not saw_text_delta and block.text:
                                assistant_text_parts.append(block.text)
                                yield json_line({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            assistant_meta.append(
                                meta_entry(
                                    f"工具 {block.name}: {compact_text(block.input)}"
                                )
                            )
                            yield json_line(
                                {
                                    "type": "tool_use",
                                    "name": block.name,
                                    "preview": compact_text(block.input),
                                }
                            )
                        elif isinstance(block, ToolResultBlock):
                            assistant_meta.append(
                                meta_entry(
                                    f"{'工具报错' if block.is_error else '工具结果'}: {compact_text(block.content)}",
                                    "error" if block.is_error else "info",
                                )
                            )
                            yield json_line(
                                {
                                    "type": "tool_result",
                                    "is_error": bool(block.is_error),
                                    "preview": compact_text(block.content),
                                }
                            )
                    continue

                if isinstance(message, TaskStartedMessage):
                    assistant_meta.append(meta_entry(f"任务开始: {message.description}"))
                    yield json_line(
                        {
                            "type": "task_started",
                            "description": message.description,
                            "task_type": message.task_type,
                        }
                    )
                    continue

                if isinstance(message, TaskProgressMessage):
                    progress_text = (
                        f"进行中: {message.description}"
                        f"{f' · {message.last_tool_name}' if message.last_tool_name else ''}"
                    )
                    assistant_meta.append(meta_entry(progress_text))
                    yield json_line(
                        {
                            "type": "task_progress",
                            "description": message.description,
                            "last_tool_name": message.last_tool_name,
                        }
                    )
                    continue

                if isinstance(message, TaskNotificationMessage):
                    assistant_meta.append(
                        meta_entry(
                            f"任务{'完成' if message.status == 'completed' else message.status}: {message.summary}",
                            "success" if message.status == "completed" else "info",
                        )
                    )
                    yield json_line(
                        {
                            "type": "task_done",
                            "status": message.status,
                            "summary": message.summary,
                        }
                    )
                    continue

                if isinstance(message, ResultMessage):
                    session.claude_session_id = message.session_id
                    if message.is_error:
                        assistant_meta.append(
                            meta_entry(
                                f"执行失败: {message.result or '未知错误'}",
                                "error",
                            )
                        )
                    else:
                        assistant_meta.append(
                            meta_entry(
                                f"完成 · {message.duration_ms} ms"
                                f"{f' · ${message.total_cost_usd}' if message.total_cost_usd else ''}",
                                "success",
                            )
                        )

                    assistant_content = "".join(assistant_text_parts).strip()
                    if not assistant_content:
                        assistant_content = message.result or ""
                    await session_manager.append_history(
                        session,
                        "assistant",
                        assistant_content,
                        assistant_meta,
                    )
                    yield json_line(
                        {
                            "type": "result",
                            "is_error": message.is_error,
                            "result": message.result,
                            "cost_usd": message.total_cost_usd,
                            "duration_ms": message.duration_ms,
                        }
                    )
                    return

                if isinstance(message, SystemMessage):
                    if message.subtype in {"init"}:
                        continue
                    yield json_line(
                        {
                            "type": "system",
                            "subtype": message.subtype,
                        }
                    )

        except Exception as exc:
            assistant_meta.append(meta_entry(str(exc), "error"))
            await session_manager.append_history(
                session,
                "assistant",
                "".join(assistant_text_parts).strip() or "请求失败，请查看错误信息。",
                assistant_meta,
            )
            yield json_line({"type": "error", "message": str(exc)})


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await session_manager.close_all()


app = FastAPI(title="Claude SDK Web Chat", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session() -> CreateSessionResponse:
    session = await session_manager.create_session()
    return session_response(session)


@app.get("/api/sessions/{session_id}", response_model=SessionStateResponse)
async def get_session(session_id: str) -> SessionStateResponse:
    state = await session_manager.get_persisted_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionStateResponse(**state)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    await session_manager.delete_session(session_id)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str) -> dict[str, str]:
    await session_manager.interrupt_session(session_id)
    return {"status": "interrupt_sent"}


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    session = await session_manager.get_session(request.session_id)
    stream = stream_chat_response(session, request.message.strip())
    return StreamingResponse(stream, media_type="application/x-ndjson")
