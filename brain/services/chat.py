"""Talking to agents, whoever is asking: the dashboard, the CLI, or any connected program."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from brain.domain.agents import Agent, AgentName, Capability
from brain.domain.conversation import (
    UNSET,
    BackendChat,
    ChatChunk,
    ChatEvent,
    ChatEventType,
    ChatMessage,
    ChatReply,
    ChatRequest,
    Conversation,
    Role,
)
from brain.domain.errors import BrainError, CapabilityMissing, InvalidRequest, NotFound
from brain.ports import ChatBackend, ConversationRepository
from brain.services.agents import AgentManager
from brain.services.devices import DeviceHub

_TITLE_LENGTH = 60


def _title_from(message: str) -> str:
    line = " ".join(message.split())
    if len(line) <= _TITLE_LENGTH:
        return line or "New conversation"
    return f"{line[:_TITLE_LENGTH - 1].rstrip()}…"


def _stats_of(chunk: ChatChunk) -> frozendict[str, Any]:
    tokens = chunk.completion_tokens
    nanos = chunk.completion_nanos
    return frozendict(
        prompt_tokens=chunk.prompt_tokens,
        completion_tokens=tokens,
        tokens_per_second=round(tokens / (nanos / 1e9), 2) if tokens and nanos else None,
        total_seconds=round(chunk.total_nanos / 1e9, 2) if chunk.total_nanos else None,
        done_reason=chunk.done_reason,
    )


class ChatService:
    def __init__(
        self,
        *,
        agents: AgentManager,
        backend: ChatBackend,
        conversations: ConversationRepository,
        devices: DeviceHub,
    ) -> None:
        self._agents = agents
        self._backend = backend
        self._conversations = conversations
        self._devices = devices

    def stream(self, request: ChatRequest) -> Iterator[ChatEvent]:
        """Validates now, so problems become proper errors before any streaming has begun."""
        self._validate(request)
        agent = self._agents.require_mounted(request.agent, auto_mount=request.auto_mount)
        if not agent.can_chat:
            raise CapabilityMissing(
                f"{agent.name} cannot hold a conversation.",
                hint="It is an embedding agent, so use its embed endpoint instead.",
            )
        conversation = self._prepare_conversation(request, agent.name)
        call = BackendChat(
            agent=agent.name,
            messages=self._assemble_messages(request, conversation),
            options=request.options,
            # Ollama rejects thinking requests sent to models that cannot reason.
            think=request.think if Capability.THINKING in agent.capabilities else None,
            format=request.format,
            tools=request.tools,
        )
        return self._run(call, conversation, agent)

    def complete(self, request: ChatRequest) -> ChatReply:
        content: list[str] = []
        thinking: list[str] = []
        tool_calls: tuple[frozendict[str, Any], ...] = ()
        stats = None
        conversation_id = request.conversation_id
        for event in self.stream(request):
            match event.type:
                case ChatEventType.CONTENT:
                    content.append(event.text)
                case ChatEventType.THINKING:
                    thinking.append(event.text)
                case ChatEventType.TOOL_CALLS:
                    tool_calls += event.tool_calls
                case ChatEventType.DONE:
                    stats = event.stats
                case ChatEventType.ERROR:
                    failure = event.error or frozendict()
                    raise BrainError(failure.get("message", "The agent failed to reply."))
        return ChatReply(
            agent=AgentName.parse(request.agent),
            content="".join(content),
            thinking="".join(thinking),
            conversation_id=conversation_id,
            tool_calls=tool_calls,
            stats=stats,
        )

    def conversations(self, *, limit: int = 100) -> tuple[Conversation, ...]:
        return self._conversations.list(limit=limit)

    def conversation(self, conversation_id: str) -> tuple[Conversation, tuple]:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFound(f"There is no conversation {conversation_id}.")
        return conversation, self._conversations.messages(conversation_id)

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        self.conversation(conversation_id)
        self._conversations.update(conversation_id, title=title.strip() or "Untitled")
        conversation = self._conversations.get(conversation_id)
        assert conversation is not None
        return conversation

    def delete_conversation(self, conversation_id: str) -> None:
        if not self._conversations.delete(conversation_id):
            raise NotFound(f"There is no conversation {conversation_id}.")

    @staticmethod
    def _validate(request: ChatRequest) -> None:
        has_message = bool(request.message and request.message.strip())
        if has_message == bool(request.messages):
            raise InvalidRequest("Send either 'message' or 'messages', not both or neither.")
        if request.messages and request.conversation_id:
            raise InvalidRequest(
                "'messages' carries its own history, so it cannot be combined with "
                "'conversation_id'. Use 'message' to continue a stored conversation."
            )

    def _prepare_conversation(self, request: ChatRequest, agent: AgentName) -> Conversation | None:
        if request.conversation_id is None:
            return None
        conversation = self._conversations.ensure(
            request.conversation_id, agent=agent, title=_title_from(request.message or "")
        )
        self._conversations.update(conversation.id, system=request.system, agent=agent)
        if request.system is not UNSET:
            conversation = replace(conversation, system=request.system)
        return conversation

    def _assemble_messages(
        self, request: ChatRequest, conversation: Conversation | None
    ) -> tuple[ChatMessage, ...]:
        system = self._system_prompt(request, conversation)
        preamble = (ChatMessage(role=Role.SYSTEM, content=system),) if system else ()
        if request.messages:
            return (*preamble, *request.messages)

        assert request.message is not None
        turn = ChatMessage(role=Role.USER, content=request.message, images=request.images)
        if conversation is None:
            return (*preamble, turn)
        history = tuple(
            ChatMessage(role=stored.role, content=stored.content)
            for stored in self._conversations.messages(conversation.id)
            # A reply stopped during its reasoning has no words, and models should not see a blank turn.
            if stored.content
        )
        self._conversations.append(
            conversation.id, role=Role.USER.value, content=request.message
        )
        return (*preamble, *history, turn)

    def _system_prompt(self, request: ChatRequest, conversation: Conversation | None) -> str | None:
        if request.system is not UNSET:
            base = request.system
        else:
            base = conversation.system if conversation else None
        sensors = self._devices.describe_for_agent() if request.sensor_context else None
        return "\n\n".join(part for part in (base, sensors) if part) or None

    def _run(
        self, call: BackendChat, conversation: Conversation | None, agent: Agent
    ) -> Iterator[ChatEvent]:
        conversation_id = conversation.id if conversation else None
        content: list[str] = []
        thinking: list[str] = []
        stats: frozendict[str, Any] | None = None
        finished = False
        try:
            yield ChatEvent(type=ChatEventType.START, conversation_id=conversation_id)
            for chunk in self._backend.chat(call):
                if chunk.thinking:
                    thinking.append(chunk.thinking)
                    yield ChatEvent(type=ChatEventType.THINKING, text=chunk.thinking)
                if chunk.content:
                    content.append(chunk.content)
                    yield ChatEvent(type=ChatEventType.CONTENT, text=chunk.content)
                if chunk.tool_calls:
                    yield ChatEvent(type=ChatEventType.TOOL_CALLS, tool_calls=chunk.tool_calls)
                if chunk.done:
                    stats = _stats_of(chunk)
            finished = True
            yield ChatEvent(
                type=ChatEventType.DONE, conversation_id=conversation_id, stats=stats
            )
        except BrainError as failure:
            yield ChatEvent(
                type=ChatEventType.ERROR,
                conversation_id=conversation_id,
                error=frozendict(code=failure.code, message=failure.message),
            )
        finally:
            # This also runs when the client disconnects, so a partial reply is never lost.
            if conversation_id is not None and (content or thinking):
                self._conversations.append(
                    conversation_id,
                    role=Role.ASSISTANT.value,
                    content="".join(content),
                    thinking="".join(thinking) or None,
                    agent=agent.name,
                    interrupted=not finished,
                    stats=dict(stats) if stats else None,
                )
