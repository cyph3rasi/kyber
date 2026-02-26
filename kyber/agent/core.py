"""Direct tool-calling agent core.

Hermes-style agent loop: LLM calls tools directly in a loop until
it produces a final text response. No intent intermediary, no OpenHands
dependency — just straightforward tool calling.

    User message → LLM → tool calls → execute → LLM → ... → final response

Plugs into kyber's existing bus/channel architecture:
    Channel → Bus → AgentCore → Bus → Channel
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from kyber.agent.context import ContextBuilder
from kyber.agent.task_registry import Task, TaskRegistry, TaskStatus
from kyber.agent.tools.registry import registry
from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from kyber.session.manager import SessionManager, Session

logger = logging.getLogger(__name__)


class AgentCore:
    """Direct tool-calling agent.
    
    Consumes messages from the bus, runs a tool-calling loop with the LLM,
    and publishes responses back to the bus.
    
    This replaces the intent-based Orchestrator with a simpler, more direct
    architecture inspired by hermes-agent.
    
    Args:
        bus: MessageBus for inbound/outbound messages.
        provider: LLM provider for chat completions.
        workspace: Path to the workspace directory.
        model: Model identifier (uses provider default if not set).
        max_iterations: Maximum tool-calling loop iterations per message.
        max_history: Maximum conversation messages to include as context.
        persona_prompt: Optional persona/personality system prompt override.
        timezone: Timezone for datetime display.
        progress_callback: Optional callback for tool execution progress updates.
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 30,
        max_history: int = 40,
        persona_prompt: str | None = None,
        timezone: str | None = None,
        task_history_path: Path | None = None,
        progress_callback: Callable[[str, str, str], Awaitable[None]] | None = None,
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.max_history = max_history
        self.persona_prompt = persona_prompt
        self.progress_callback = progress_callback
        
        # Core components
        self.context = ContextBuilder(workspace, timezone=timezone)
        self.sessions = SessionManager(workspace)
        self.registry = TaskRegistry(history_path=task_history_path)
        
        # Discover and register all tools
        registry.discover()
        self._configure_tool_callbacks()
        
        # State
        self._running = False
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
    
    # ── Public API ──────────────────────────────────────────────────
    
    async def run(self) -> None:
        """Main message processing loop. Consumes from bus indefinitely."""
        self._running = True
        logger.info(f"AgentCore started (model={self.model}, tools={len(registry)})")
        
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(), timeout=1.0
                )
                # Process each message concurrently
                task = asyncio.create_task(self._handle_message(msg))
                self._active_tasks[msg.session_key] = task
                task.add_done_callback(
                    lambda t, key=msg.session_key: self._active_tasks.pop(key, None)
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")
                await asyncio.sleep(1)
    
    def stop(self) -> None:
        """Signal the agent to stop processing."""
        self._running = False
        # Cancel any active tasks
        for task in self._active_tasks.values():
            task.cancel()
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:default",
        channel: str = "cli",
        chat_id: str = "default",
    ) -> str:
        """Process a message directly (for CLI/cron, bypassing the bus).
        
        Args:
            content: Message text.
            session_key: Session identifier.
            channel: Channel name.
            chat_id: Chat identifier.
        
        Returns:
            Agent's text response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
        )
        return await self._process_message(msg, session_key)

    def _configure_tool_callbacks(self) -> None:
        """Attach internal callback handlers to tools that rely on the agent runtime."""
        msg_tool = registry.get("message")
        if msg_tool and hasattr(msg_tool, "set_send_callback"):
            msg_tool.set_send_callback(self._publish_tool_message)

    async def _publish_tool_message(self, outbound: OutboundMessage) -> None:
        """Publish outbound tool messages back through the message bus."""
        await self.bus.publish_outbound(outbound)
    
    # ── Internal ────────────────────────────────────────────────────
    
    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle an inbound message: process and publish response."""
        try:
            response_text = await self._process_message(msg, msg.session_key)
            
            # Publish response to bus
            response = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=response_text,
            )
            await self.bus.publish_outbound(response)
            
        except Exception as e:
            logger.exception(f"Error handling message from {msg.session_key}")
            error_msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I hit an error: {str(e)}",
            )
            await self.bus.publish_outbound(error_msg)
    
    async def _process_message(self, msg: InboundMessage, session_key: str) -> str:
        """Core message processing: build context, run tool loop, return response."""
        
        # Get or create session
        session = self.sessions.get_or_create(session_key)
        
        # Add user message to session
        session.add_message("user", msg.content)
        
        # Build system prompt
        system_prompt = self._build_system_prompt(msg)
        
        # Get tool definitions
        tool_defs = registry.get_definitions()
        
        # Build messages for LLM
        messages = self._build_messages(system_prompt, session)
        
        # Run the tool-calling loop
        response_text = await self._run_loop(
            messages=messages,
            tools=tool_defs,
            session=session,
            context_channel=msg.channel,
            context_chat_id=msg.chat_id,
        )
        
        # Add assistant response to session
        session.add_message("assistant", response_text)
        
        # Save session
        self.sessions.save(session)
        
        return response_text

    def _spawn_task(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> None:
        """Run a task asynchronously from the legacy task API."""
        del conversation_context
        del project_key

        if not task:
            return

        run_key = session_key or f"task:{task.id}"

        async def _runner() -> None:
            self.registry.mark_started(task.id)
            try:
                result = await self.process_direct(
                    task.description,
                    session_key=run_key,
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                )
            except asyncio.CancelledError:
                self.registry.mark_cancelled(task.id, "Cancelled by user")
                raise
            except Exception as exc:  # pragma: no cover
                logger.exception("Background task failed: %s", task.id)
                self.registry.mark_failed(task.id, str(exc))
                await self.bus.publish_outbound(OutboundMessage(
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    content=f"{task.label} failed: {exc}",
                    is_background=True,
                ))
                return

            final_text = (result or "").strip() or "Done."
            if task.status != TaskStatus.CANCELLED:
                self.registry.mark_completed(task.id, final_text)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    content=final_text,
                    is_background=True,
                ))

        bg_task = asyncio.create_task(_runner())

        def _done(_: asyncio.Task) -> None:
            self._background_tasks.pop(task.id, None)

        bg_task.add_done_callback(_done)
        self._background_tasks[task.id] = bg_task

    def _cancel_task(self, task_id: str) -> bool:
        """Cancel a background task by task id."""
        running_task = self._background_tasks.get(task_id)
        if not running_task:
            return False

        running_task.cancel()
        self.registry.mark_cancelled(task_id, "Cancelled by user")
        return True
    
    async def _run_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session: Session,
        context_channel: str = "",
        context_chat_id: str = "",
    ) -> str:
        """The core hermes-style tool-calling loop.
        
        Calls the LLM, executes any tool calls, feeds results back,
        and loops until the LLM produces a final text response.
        """
        iteration = 0
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Call LLM
            try:
                response = await self.provider.chat(
                    messages=messages,
                    tools=tools if tools else None,
                    model=self.model,
                )
            except Exception as e:
                logger.error(f"LLM call failed (iteration {iteration}): {e}")
                return f"I had trouble reaching the AI model: {str(e)}"
            
            # If the LLM returned tool calls — execute them
            if response.has_tool_calls:
                # Add assistant message with tool calls to conversation
                assistant_msg = self._build_tool_call_message(response)
                messages.append(assistant_msg)
                
                # Execute each tool call
                for tc in response.tool_calls:
                    logger.info(f"Tool call: {tc.name}({_summarize_args(tc.arguments)})")
                    
                    # Send progress update if callback registered
                    if self.progress_callback:
                        try:
                            await self.progress_callback(
                                context_channel,
                                context_chat_id,
                                f"🔧 {tc.name}...",
                            )
                        except Exception:
                            pass
                    
                    # Execute the tool
                    result = await self._execute_tool(
                        tc,
                        context_channel,
                        context_chat_id,
                        task_id=session.key,
                    )
                    
                    # Add tool result to conversation
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                    messages.append(tool_msg)
                    
                    # Also record in session for persistence
                    session.add_message(
                        "tool",
                        result,
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                    )
                
                # Loop back to call LLM again with tool results
                continue
            
            # No tool calls — we have a final text response
            if response.content:
                return response.content
            
            # Edge case: no content and no tool calls
            logger.warning(f"Empty response from LLM at iteration {iteration}")
            if iteration >= self.max_iterations:
                break
            continue
        
        # Exceeded max iterations
        logger.warning(f"Agent loop hit max iterations ({self.max_iterations})")
        return "I ran out of steps trying to complete that. Could you try breaking it into smaller pieces?"
    
    async def _execute_tool(
        self,
        tc: ToolCallRequest,
        channel: str = "",
        chat_id: str = "",
        task_id: str = "",
    ) -> str:
        """Execute a single tool call and return the result string."""
        try:
            # Special handling for message tool — set context
            msg_tool = registry.get("message")
            if msg_tool and hasattr(msg_tool, "set_context"):
                msg_tool.set_context(channel, chat_id)
            
            result = await registry.execute(tc.name, tc.arguments, task_id=task_id)
            
            # Truncate very long results
            if len(result) > 100_000:
                result = result[:100_000] + f"\n... (truncated, {len(result) - 100_000} more chars)"
            
            return result
            
        except Exception as e:
            logger.exception(f"Tool execution error: {tc.name}")
            return json.dumps({"error": str(e)})
    
    def _build_system_prompt(self, msg: InboundMessage) -> str:
        """Build the system prompt, optionally with persona overlay."""
        # Start with the context builder's prompt
        base_prompt = self.context.build_system_prompt()
        
        parts = []
        
        # If we have a custom persona (from SOUL.md etc), use it
        if self.persona_prompt:
            parts.append(self.persona_prompt)
        
        parts.append(base_prompt)
        
        # Add channel context
        parts.append(
            f"\n## Current Context\n"
            f"Channel: {msg.channel}\n"
            f"Chat ID: {msg.chat_id}\n"
            f"User: {msg.sender_id}"
        )
        
        return "\n\n".join(parts)
    
    def _build_messages(
        self,
        system_prompt: str,
        session: Session,
    ) -> list[dict[str, Any]]:
        """Build the messages list for the LLM call."""
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add conversation history (skip the last message which we just added)
        history = session.get_history(max_messages=self.max_history)
        
        # Filter to just user/assistant messages for now
        # (tool messages from previous turns don't need to go back)
        for msg in history:
            if msg["role"] in ("user", "assistant"):
                messages.append(msg)
        
        return messages
    
    def _build_tool_call_message(self, response: LLMResponse) -> dict[str, Any]:
        """Build an assistant message dict with tool calls (OpenAI format)."""
        tool_calls = []
        for tc in response.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            })
        
        msg: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": tool_calls,
        }
        
        # Include content if present (some models return both)
        if response.content:
            msg["content"] = response.content
        else:
            msg["content"] = None
        
        return msg


def _summarize_args(args: dict[str, Any], max_len: int = 80) -> str:
    """Summarize tool arguments for logging."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[:max_len - 3] + "..."
    return summary
