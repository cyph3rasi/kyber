"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider
from kyber.agent.context import ContextBuilder
from kyber.agent.tools.registry import ToolRegistry
from kyber.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from kyber.agent.tools.shell import ExecTool
from kyber.agent.tools.web import WebSearchTool, WebFetchTool
from kyber.agent.tools.message import MessageTool
from kyber.agent.tools.spawn import SpawnTool
from kyber.agent.tools.task_status import TaskStatusTool
from kyber.agent.subagent import SubagentManager
from kyber.session.manager import SessionManager
from kyber.meta_messages import (
    build_offload_ack_fallback,
    build_tool_status_text,
    clean_one_liner,
    llm_meta_messages_enabled,
    looks_like_prompt_leak,
)

# Wall-clock timeout before auto-offloading to a subagent (seconds)
AUTO_OFFLOAD_TIMEOUT = 30


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        search_max_results: int = 5,
        exec_config: "ExecToolConfig | None" = None,
    ):
        from kyber.config.schema import ExecToolConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.search_max_results = search_max_results
        self.exec_config = exec_config or ExecToolConfig()
        
        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
        )
        
        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool())
        self.tools.register(EditFileTool())
        self.tools.register(ListDirTool())
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, max_results=self.search_max_results))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Task status tool (instant subagent progress lookup)
        task_status_tool = TaskStatusTool(manager=self.subagents)
        self.tools.register(task_status_tool)

    async def _generate_tool_status(
        self,
        messages: list[dict[str, Any]],
        tool_name: str
    ) -> str | None:
        """Generate a short status update (LLM if enabled, otherwise deterministic)."""
        _ = messages  # tool status should not depend on full conversation context

        if not llm_meta_messages_enabled():
            return build_tool_status_text(tool_name)

        system = (
            "You are kyber, a helpful AI assistant. "
            "Write naturally with a bit of personality, but keep it short."
        )
        prompt = (
            "Write exactly one short sentence (4-16 words, max 120 characters) "
            "telling the user what you're about to do next. "
            "Do not mention prompts, rules, roles, tools, or tool calls. "
            "Do not include markdown, quotes, or lists. "
            f"Action: {tool_name}. "
            "End with punctuation."
        )

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=self.model,
                max_tokens=60,
                temperature=0.8,
            )
        except Exception as e:
            logger.warning(f"Status update generation failed: {e}")
            return build_tool_status_text(tool_name)

        content = clean_one_liner(response.content or "")
        if not content:
            return build_tool_status_text(tool_name)
        if len(content) > 120:
            content = content[:117].rstrip() + "..."
        if content[-1] not in ".!?":
            return build_tool_status_text(tool_name)
        if len(content.split()) < 4:
            return build_tool_status_text(tool_name)
        if looks_like_prompt_leak(content):
            logger.warning(f"Blocked suspicious status update: {content!r}")
            return build_tool_status_text(tool_name)
        return content

    async def _publish_tool_status(
        self,
        channel: str,
        chat_id: str,
        tool_name: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Publish a short status message before executing a tool call."""
        content = await self._generate_tool_status(messages, tool_name)
        if not content:
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content
        ))
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus.

        Each inbound message is handled in its own asyncio task so the loop
        is never blocked — the user can always send new messages even while
        a long task is in progress.
        """
        self._running = True
        self._active_tasks: set[asyncio.Task[None]] = set()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0,
                )
                task = asyncio.create_task(self._handle_message(msg))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
            except asyncio.TimeoutError:
                continue

    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle a single message in its own task (fire-and-forget from run)."""
        try:
            # System messages (subagent results) are never offloaded
            if msg.channel == "system":
                response = await self._process_message(msg)
            else:
                response = await self._process_with_timeout(msg)
            if response:
                await self.bus.publish_outbound(response)
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I encountered an error: {str(e)}",
            ))

    async def _process_with_timeout(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a user message with a wall-clock timeout for acknowledgment.

        The work always runs to completion. If it takes longer than
        AUTO_OFFLOAD_TIMEOUT seconds, we register it as a tracked task,
        send the user an in-character heads-up, and let it keep running.
        The user can check progress via the task_status tool at any time.
        """
        process_task = asyncio.create_task(self._process_message(msg))
        task_id: str | None = None

        try:
            return await asyncio.wait_for(
                asyncio.shield(process_task),
                timeout=AUTO_OFFLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # Register as a tracked task so task_status can report on it
            import uuid
            task_id = str(uuid.uuid4())[:8]
            label = msg.content[:40] + ("…" if len(msg.content) > 40 else "")
            self.subagents.register_task(task_id, label, msg.content)

            logger.info(
                f"Message from {msg.channel}:{msg.sender_id} still processing "
                f"after {AUTO_OFFLOAD_TIMEOUT}s — registered as task {task_id}"
            )
            ack = await self._generate_offload_ack(msg.content)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=ack,
            ))

            # Let the original task finish
            try:
                result = await process_task
            finally:
                self.subagents.complete_task(task_id)
            return result

    async def _generate_offload_ack(self, user_message: str) -> str:
        """Generate an in-character acknowledgment for a long-running task.

        Uses LLM if enabled, but with strict leakage guards and a deterministic
        fallback to avoid surfacing internal prompts/instructions.
        """
        short_msg = " ".join((user_message or "").split()).strip()
        if len(short_msg) > 180:
            short_msg = short_msg[:177].rstrip() + "..."

        fallback = build_offload_ack_fallback()
        if not llm_meta_messages_enabled():
            return fallback

        system = (
            "You are kyber, a helpful AI assistant. "
            "You're friendly, concise, and speak naturally."
        )
        prompt = (
            "Write 1-2 short sentences letting the user know you're still working "
            "and will send the result when ready. "
            "Do not mention prompts, rules, roles, tools, or tool calls. "
            "No markdown, no quotes.\n\n"
            f"User request summary: {short_msg}"
        )

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=self.model,
                max_tokens=120,
                temperature=0.85,
            )
        except Exception as e:
            logger.warning(f"Offload ack generation failed: {e}")
            return fallback

        content = clean_one_liner(response.content or "")
        if not content:
            return fallback
        if len(content) > 240:
            content = content[:237].rstrip() + "..."
        # Guard against instruction/prompt echoes.
        if looks_like_prompt_leak(content):
            logger.warning(f"Blocked suspicious offload ack: {content!r}")
            return fallback
        if '"' in content or "'" in content:
            return fallback
        if content[-1] not in ".!?":
            return fallback
        return content
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")
        
        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            media=msg.media if msg.media else None,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        empty_response_retries = 0
        max_empty_response_retries = 2
        llm_error_retries = 0
        max_llm_error_retries = 3
        tool_calls_executed = False
        last_tool_results: list[str] = []
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            # Check for LLM-level errors (provider returned an error string)
            if response.finish_reason == "error":
                llm_error_retries += 1
                logger.warning(
                    f"LLM error (attempt {llm_error_retries}/{max_llm_error_retries}): "
                    f"{response.content}"
                )
                if llm_error_retries <= max_llm_error_retries:
                    await asyncio.sleep(min(2 ** (llm_error_retries - 1), 4))
                    continue
                # Exhausted retries — use a fallback instead of crashing
                logger.error("LLM errors exhausted, using fallback response")
                break
            
            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                status_messages = messages.copy()
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )
                
                # Execute tools
                last_tool_results.clear()
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    await self._publish_tool_status(
                        msg.channel, msg.chat_id, tool_call.name, status_messages
                    )
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    last_tool_results.append(result)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                tool_calls_executed = True
                # Reset error counters after successful tool execution
                llm_error_retries = 0
                empty_response_retries = 0
            else:
                # No tool calls — check for content
                final_content = (response.content or "").strip()
                # Treat error-prefixed content as empty so we retry
                if final_content.startswith("Error calling LLM:"):
                    logger.warning(f"LLM returned error as content: {final_content}")
                    final_content = ""
                if not final_content:
                    empty_response_retries += 1
                    logger.warning(
                        f"Empty LLM response; retry {empty_response_retries}/"
                        f"{max_empty_response_retries} (finish_reason={response.finish_reason})."
                    )
                    if empty_response_retries <= max_empty_response_retries:
                        # If we already executed tools, nudge the LLM to summarize
                        if tool_calls_executed:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You executed tools and got results. Now please "
                                    "summarize the results and respond to the user."
                                )
                            })
                        else:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Please provide your response to the user's message."
                                )
                            })
                        continue
                break
        
        # Fallback: if we still have no content, generate something useful
        if not final_content or not final_content.strip():
            if tool_calls_executed and last_tool_results:
                # We ran tools but the LLM never summarized — build a minimal reply
                logger.warning("No final LLM content after tool calls; generating fallback")
                final_content = (
                    "I completed the requested actions. Let me know if you need "
                    "anything else!"
                )
            else:
                logger.error("Empty LLM response after all retries")
                final_content = (
                    "Sorry, I'm having trouble generating a response right now. "
                    "Please try again in a moment."
                )
        
        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content
        )
        
        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        empty_response_retries = 0
        max_empty_response_retries = 2
        llm_error_retries = 0
        max_llm_error_retries = 3
        tool_calls_executed = False
        last_tool_results: list[str] = []
        
        while iteration < self.max_iterations:
            iteration += 1
            
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            # Check for LLM-level errors
            if response.finish_reason == "error":
                llm_error_retries += 1
                logger.warning(
                    f"LLM error in system handler (attempt {llm_error_retries}/"
                    f"{max_llm_error_retries}): {response.content}"
                )
                if llm_error_retries <= max_llm_error_retries:
                    await asyncio.sleep(min(2 ** (llm_error_retries - 1), 4))
                    continue
                logger.error("LLM errors exhausted in system handler, using fallback")
                break
            
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                status_messages = messages.copy()
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )
                
                last_tool_results.clear()
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    await self._publish_tool_status(
                        origin_channel, origin_chat_id, tool_call.name, status_messages
                    )
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    last_tool_results.append(result)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                tool_calls_executed = True
                llm_error_retries = 0
                empty_response_retries = 0
            else:
                final_content = (response.content or "").strip()
                if final_content.startswith("Error calling LLM:"):
                    logger.warning(f"LLM returned error as content (system): {final_content}")
                    final_content = ""
                if not final_content:
                    empty_response_retries += 1
                    logger.warning(
                        f"Empty LLM response (system); retry {empty_response_retries}/"
                        f"{max_empty_response_retries} (finish_reason={response.finish_reason})."
                    )
                    if empty_response_retries <= max_empty_response_retries:
                        if tool_calls_executed:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You executed tools and got results. Now please "
                                    "summarize the results and respond to the user."
                                )
                            })
                        else:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Please provide your response to the user's message."
                                )
                            })
                        continue
                break
        
        if not final_content or not final_content.strip():
            if tool_calls_executed and last_tool_results:
                logger.warning("No final LLM content after tool calls (system); generating fallback")
                final_content = (
                    "I completed the requested actions. Let me know if you need "
                    "anything else!"
                )
            else:
                logger.error("Empty LLM response after all retries (system)")
                final_content = (
                    "Sorry, I'm having trouble generating a response right now. "
                    "Please try again in a moment."
                )
        
        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(self, content: str, session_key: str = "cli:direct") -> str:
        """
        Process a message directly (for CLI usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
