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
        
        # Let the context builder see active tasks so the LLM knows what's in flight
        self.context.set_task_status_provider(self.subagents.get_all_status)
        
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

    # How long to wait for a response before sending an interim "still working" message
    RESPONSE_TIMEOUT = 10.0  # seconds

    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle a single message in its own task (fire-and-forget from run).
        
        If processing takes longer than RESPONSE_TIMEOUT seconds, sends an
        interim message so the user isn't left waiting in silence. The original
        task keeps running and will deliver its response when done.
        """
        try:
            process_task = asyncio.create_task(self._process_message(msg))
            interim_sent = False

            try:
                response = await asyncio.wait_for(
                    asyncio.shield(process_task),
                    timeout=self.RESPONSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Processing is still running — send interim message, don't cancel
                interim_sent = True
                logger.info(
                    f"Response timeout ({self.RESPONSE_TIMEOUT}s) for "
                    f"{msg.channel}:{msg.sender_id}, sending interim message"
                )

                # Register as a tracked task so status queries work
                task_label = msg.content[:60] + ("…" if len(msg.content) > 60 else "")
                progress = self.subagents.register_task(
                    task_id=f"msg-{id(process_task):x}"[:12],
                    label=task_label,
                    task=msg.content,
                )

                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="💎 Processing in background. You can continue chatting, or ask for status updates.",
                ))

                # Now wait for the original task to finish (no timeout)
                response = await process_task

                # Mark tracked task as done
                self.subagents.complete_task(progress.task_id)

            if response and not interim_sent:
                # Normal fast path — send response directly
                await self.bus.publish_outbound(response)
            elif response and interim_sent:
                # Task finished after interim — send the actual response
                await self.bus.publish_outbound(response)

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I encountered an error: {str(e)}",
            ))


    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    @staticmethod
    def _parse_slash_command(content: str) -> tuple[str | None, str]:
        """Parse a slash command from the message content.
        
        Returns:
            (command, cleaned_content) — command is None if no slash command found.
            The cleaned content has the /command stripped out.
        """
        stripped = content.strip()
        # Check for /bg at the start (with optional space after)
        if stripped.lower().startswith("/bg"):
            rest = stripped[3:].lstrip()
            return "bg", rest if rest else stripped[3:]
        return None, content
    
    async def _handle_bg_command(self, msg: InboundMessage) -> OutboundMessage:
        """Handle /bg — spawn a subagent directly without LLM round-trip."""
        logger.info(f"/bg command from {msg.channel}:{msg.sender_id}")
        
        # Set spawn context
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        task = msg.content.strip()
        if not task:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="What do you want me to work on in the background? Use: /bg <task>",
            )
        
        # Build full context (system prompt + history) so the subagent is a full clone
        session = self.sessions.get_or_create(msg.session_key)
        context_messages = self.context.build_messages(
            history=session.get_history(),
            current_message="",  # task will be appended by the subagent runner
        )
        # Remove the empty user message we just added (subagent adds its own)
        if context_messages and context_messages[-1].get("role") == "user":
            context_messages.pop()
        
        # Call spawn directly with full context
        if isinstance(spawn_tool, SpawnTool):
            result = await spawn_tool._manager.spawn(
                task=task,
                origin_channel=msg.channel,
                origin_chat_id=msg.chat_id,
                context_messages=context_messages,
            )
            logger.info(f"/bg spawned: {result}")
        else:
            logger.error("SpawnTool not available")
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Background tasks aren't available right now.",
            )
        
        # Save to session
        session.add_message("user", f"/bg {task}")
        ack = f"On it — working on that in the background. I'll report back when it's done."
        session.add_message("assistant", ack)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=ack,
        )
    
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
        
        # Parse slash commands
        slash_cmd, cleaned_content = self._parse_slash_command(msg.content)
        if cleaned_content != msg.content:
            msg.content = cleaned_content
        
        # /bg — force background execution via spawn, skip the LLM round-trip
        if slash_cmd == "bg":
            return await self._handle_bg_command(msg)
        
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
        did_spawn = False
        
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
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    last_tool_results.append(result)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                tool_calls_executed = True
                # Reset error counters after successful tool execution
                llm_error_retries = 0
                empty_response_retries = 0
                
                # If the model called spawn, let the loop continue so the model
                # sees the spawn tool result and can write a proper follow-up message.
                spawned = any(tc.name == "spawn" for tc in response.tool_calls)
                if spawned:
                    did_spawn = True
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
