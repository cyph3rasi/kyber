"""
Worker: Executes tasks in the background with guaranteed completion.

Workers run async, emit progress events, and always complete (success or failure).
The completion queue guarantees the user gets notified.
"""

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.agent.task_registry import Task, TaskRegistry
from kyber.agent.workspace_index import WorkspaceIndex
from kyber.agent.narrator import LiveNarrator
from kyber.meta_messages import looks_like_prompt_leak
from kyber.providers.base import LLMProvider
from kyber.utils.helpers import redact_secrets


class Worker:
    """
    Executes a single task with tools.
    
    Guarantees:
    - Always completes (success, failure, or timeout)
    - Emits progress updates
    - Pushes to completion queue when done
    """

    _opencode_update_lock: asyncio.Lock | None = None
    _opencode_server_lock: asyncio.Lock | None = None
    _opencode_server_process: asyncio.subprocess.Process | None = None
    _opencode_server_signature: str | None = None
    _CODESEARCH_CHUNK_ERROR_MARKERS = (
        "separator is found, but chunk is longer than limit",
        "chunk is longer than limit",
        "chunk longer than limit",
        "chunk exceeds",
        "text chunk",
    )
    _MAX_BOOTSTRAP_FILE_CHARS = 2000
    _MAX_BOOTSTRAP_TOTAL_CHARS = 6000
    _MAX_WORKSPACE_INDEX_CHARS = 5000
    _codesearch_disabled_profiles: set[str] = set()
    
    def __init__(
        self,
        task: Task,
        provider: LLMProvider,
        workspace: Path,
        registry: TaskRegistry,
        completion_queue: asyncio.Queue,
        persona_prompt: str,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_timeout: int = 60,
        timezone: str | None = None,
        workspace_index: WorkspaceIndex | None = None,
        narrator: LiveNarrator | None = None,
    ):
        self.task = task
        self.provider = provider
        self.workspace = workspace
        self.registry = registry
        self.completion_queue = completion_queue
        self.persona_prompt = persona_prompt
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_timeout = exec_timeout
        self.timezone = timezone
        self.workspace_index = workspace_index
        self.narrator = narrator

    @staticmethod
    def _result_is_unusable(text: str) -> bool:
        """Check if a final result is unsuitable for user delivery."""
        import re
        raw = text or ""
        t = " ".join(raw.split()).strip()
        if not t:
            return True
        if looks_like_prompt_leak(t):
            return True
        lower = t.lower()
        if len(t.split()) < 6:
            return True
        if any(tok in lower for tok in ["tool call", "tool_calls", "system prompt", "developer message"]):
            return True
        if lower.startswith("error calling llm:") or "pydantic_ai" in lower or "badrequest" in lower.replace(" ", ""):
            return True
        # Bot should never ask for permission — it should just do the work.
        if any(phrase in lower for phrase in [
            "would you like me to",
            "shall i ",
            "do you want me to",
            "want me to proceed",
            "like me to proceed",
            "like me to apply",
            "like me to fix",
        ]):
            return True
        # Markdown headers in chat messages = report-style, not conversational.
        # Check the raw text (before whitespace normalization) to preserve newlines.
        if re.search(r'^#{1,3}\s', raw, re.MULTILINE) and raw.count('\n') > 3:
            return True
        # Unfilled template placeholders — brackets containing instruction-like
        # text. Matches both "[Identify the error]" and "[specific issue here]".
        if re.search(r'\[[a-zA-Z][\w\s]{4,}\]', t):
            return True
        # Excessively long responses usually mean the LLM dumped raw file
        # contents or tool output instead of summarizing.
        if len(t) > 2000:
            return True
        return False

    async def _rewrite_result(self, raw_result: str) -> str:
        """Rewrite an unusable result into a concise user-facing message."""
        import inspect

        recent_actions = "\n".join(f"- {a}" for a in (self.task.actions_completed[-12:] or []))
        if not recent_actions:
            recent_actions = "- (none yet)"

        # Build a brief summary of what actually happened from the raw result.
        # Truncate the raw result so the rewrite LLM sees the key info without
        # being tempted to copy-paste it all.
        evidence = raw_result[:1500] if raw_result else "(no output)"

        # Pre-detect failure signals in the evidence so the LLM can't ignore them.
        failure_signals = []
        evidence_lower = evidence.lower()
        if "exit code:" in evidence_lower and "exit code: 0" not in evidence_lower:
            failure_signals.append("COMMAND FAILED (non-zero exit code)")
        if "traceback" in evidence_lower:
            failure_signals.append("PYTHON TRACEBACK DETECTED")
        if "error:" in evidence_lower or "error :" in evidence_lower:
            failure_signals.append("ERROR IN OUTPUT")
        if "not found" in evidence_lower:
            failure_signals.append("FILE OR COMMAND NOT FOUND")

        failure_context = ""
        if failure_signals:
            failure_context = (
                "\n\nFAILURE DETECTED: " + ", ".join(failure_signals) + "\n"
                "The task FAILED. Your message MUST say it failed and why. "
                "Do NOT say it succeeded. Do NOT invent positive results."
            )

        system = (
            f"{self.persona_prompt}\n\n"
            "You are rewriting a task result into a concise status update for the user.\n"
            "Stay in character.\n"
            "RULES:\n"
            "- If the output contains errors, tracebacks, or non-zero exit codes, the task FAILED. Say so.\n"
            "- Do NOT fabricate success. Do NOT say 'executed successfully' unless the output proves it.\n"
            "- Do NOT include API keys, tokens, passwords, or credentials.\n"
            "- Do NOT paste raw file contents, source code, tracebacks, or command output.\n"
            "- Do NOT use markdown headers (##) or structured formatting.\n"
            "- Do NOT ask the user for permission ('Would you like me to...').\n"
            "- Do NOT use placeholder brackets like [error here].\n"
            "- State the specific error in plain language (e.g., 'The script crashed because X').\n"
            "- Keep it under 800 characters. Talk naturally."
        )
        user = (
            f"Task: {self.task.label}\n"
            f"Request: {self.task.description}\n\n"
            f"Actions taken:\n{recent_actions}\n\n"
            f"Raw output (truncated):\n{evidence}\n"
            f"{failure_context}\n\n"
            "Rewrite this into a brief, natural status update."
        )

        try:
            kwargs: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                sig = inspect.signature(self.provider.chat)
                if "session_id" in sig.parameters:
                    kwargs["session_id"] = f"worker:{self.task.id}:rewrite"
            r = await self.provider.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=None,
                model=self.model,
                max_tokens=600,
                **kwargs,
            )
            out = (r.content or "").strip()
            if out and not self._result_is_unusable(out):
                return out
        except Exception as e:
            logger.warning(f"Worker [{self.task.id}] result rewrite failed: {e}")

        return raw_result

    def _resolve_instruction_files(self) -> list[str]:
        """Resolve stable instruction files to load through OpenCode config."""
        files: list[str] = []
        for name in ("AGENTS.md", "USER.md", "TOOLS.md", "IDENTITY.md", "SOUL.md"):
            p = self.workspace / name
            if p.exists() and p.is_file():
                files.append(str(p))
        return files
    
    def _build_worker_prompt(self) -> str:
        """Build the task prompt passed to OpenCode."""
        from kyber.utils.helpers import current_datetime_str

        workspace_index = self._get_workspace_index_block()
        workspace_index_text = workspace_index if workspace_index else "(workspace index unavailable)"

        return (
            f"{self.persona_prompt}\n\n"
            "You are executing a background task for the user.\n"
            f"Current time: {current_datetime_str(self.timezone)}\n"
            f"Workspace: {self.workspace}\n\n"
            "Execution requirements:\n"
            "- Perform the requested work directly (edit files, run commands, verify outcomes).\n"
            "- Do not ask for permission; proceed autonomously.\n"
            "- If a command fails, diagnose and fix, then retry when appropriate.\n"
            "- Keep updates concise and factual.\n"
            "- Do not leak secrets (API keys, tokens, passwords).\n"
            "- Final response must be a concise natural-language status update (<1200 chars).\n"
            "- Do not dump raw file contents, tracebacks, or large command output.\n\n"
            "Instruction files (AGENTS/USER/TOOLS/IDENTITY/SOUL) are loaded via OpenCode config.\n\n"
            f"{workspace_index_text}\n\n"
            f"Task:\n{self.task.description}\n"
        )

    def _build_worker_prompt_compact(self) -> str:
        """Build a compact worker prompt for retry paths."""
        from kyber.utils.helpers import current_datetime_str

        return (
            f"{self.persona_prompt}\n\n"
            "You are executing a background task for the user.\n"
            f"Current time: {current_datetime_str(self.timezone)}\n"
            f"Workspace: {self.workspace}\n\n"
            "Execution requirements:\n"
            "- Perform the requested work directly.\n"
            "- Do not ask for permission.\n"
            "- Keep updates concise and factual.\n"
            "- Final response must be a concise natural-language status update.\n\n"
            f"Task:\n{self.task.description}\n"
        )

    def _get_workspace_index_block(self) -> str:
        """Get workspace index for injection into system prompt."""
        if not self.workspace_index:
            return ""
        try:
            index = self.workspace_index.get()
            if index:
                if len(index) > self._MAX_WORKSPACE_INDEX_CHARS:
                    index = index[: self._MAX_WORKSPACE_INDEX_CHARS - 1].rstrip() + "…"
                return (
                    "## Workspace File Map\n"
                    "You already know the workspace layout. Skip list_dir unless you need "
                    "a subdirectory not shown here. Jump straight to the work.\n\n"
                    f"{index}"
                )
        except Exception as e:
            logger.debug(f"Failed to get workspace index: {e}")
        return ""
    
    async def run(self) -> None:
        """
        Execute the task. Guaranteed to complete or fail.
        Always pushes to completion queue.
        """
        try:
            self.registry.mark_started(self.task.id)
            start_action = "starting task"
            self.registry.update_progress(self.task.id, iteration=0, current_action=start_action)
            if self.narrator:
                self.narrator.narrate(self.task.id, start_action)
            logger.info(f"Worker started: {self.task.label} [{self.task.id}]")
            
            result = await self._execute()
            
            self.registry.mark_completed(self.task.id, result)
            logger.info(f"Worker completed: {self.task.label} [{self.task.id}]")
            
        except asyncio.CancelledError:
            self.registry.mark_cancelled(self.task.id, "cancelled")
            logger.info(f"Worker cancelled: {self.task.label} [{self.task.id}]")
            # Do not re-raise cancellation. For inline executions, bubbling
            # CancelledError can prevent a final user message and leave channel
            # typing indicators stuck.
        except Exception as e:
            error = str(e)
            self.registry.mark_failed(self.task.id, error)
            logger.error(f"Worker failed: {self.task.label} [{self.task.id}] - {e}")
        
        finally:
            # ALWAYS push to completion queue - guaranteed delivery
            await self.completion_queue.put(self.task)
    
    async def _execute(self) -> str:
        """Execute the task via provider-native task execution."""
        execute_task = getattr(self.provider, "execute_task", None)
        if not callable(execute_task):
            raise RuntimeError("Task provider does not implement execute_task().")

        kwargs: dict[str, Any] = {}
        try:
            sig = inspect.signature(execute_task)
            supports_callback = "callback" in sig.parameters
        except Exception:
            supports_callback = False

        if supports_callback:
            progress_step = max(1, int(self.task.iteration))

            async def _progress_cb(msg: str) -> None:
                nonlocal progress_step
                text = (msg or "").strip()
                if not text:
                    return
                progress_step += 1
                self.registry.update_progress(
                    self.task.id,
                    iteration=progress_step,
                    current_action=text,
                    action_completed=text,
                )
                if self.narrator:
                    self.narrator.narrate(self.task.id, text)

            kwargs["callback"] = _progress_cb

        result = await execute_task(
            task_description=self.task.description,
            persona_prompt=self.persona_prompt,
            timezone=self.timezone,
            workspace=self.workspace,
            **kwargs,
        )
        result = redact_secrets(result)
        if self._result_is_unusable(result):
            logger.info(f"Worker [{self.task.id}] result unusable, rewriting")
            result = await self._rewrite_result(result)
        return redact_secrets(result)

    def _managed_opencode_path(self) -> Path:
        """Path to Kyber-managed OpenCode binary."""
        from kyber.utils.helpers import ensure_dir, get_data_path

        bin_dir = ensure_dir(get_data_path() / "runtime" / "opencode" / "bin")
        name = "opencode.exe" if os.name == "nt" else "opencode"
        return bin_dir / name

    def _managed_opencode_manifest_path(self) -> Path:
        """Path to Kyber-managed OpenCode metadata manifest."""
        from kyber.utils.helpers import ensure_dir, get_data_path

        runtime_dir = ensure_dir(get_data_path() / "runtime" / "opencode")
        return runtime_dir / "manifest.json"

    def _opencode_runtime_home(self) -> Path:
        """Isolated HOME used for deterministic OpenCode execution."""
        from kyber.utils.helpers import ensure_dir, get_data_path

        return ensure_dir(get_data_path() / "runtime" / "opencode" / "home")

    def _opencode_release_asset_name(self) -> str:
        """Compute OpenCode release asset name for current platform."""
        system = platform.system().lower()
        machine = platform.machine().lower()
        arch_alias = {
            "amd64": "x64",
            "x86_64": "x64",
            "x64": "x64",
            "x86-64": "x64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }
        arch = arch_alias.get(machine, machine)

        if system == "darwin":
            if arch not in {"arm64", "x64"}:
                raise RuntimeError(f"Unsupported macOS architecture for OpenCode: {machine}")
            return f"opencode-darwin-{arch}.zip"
        if system == "linux":
            if arch not in {"arm64", "x64"}:
                raise RuntimeError(f"Unsupported Linux architecture for OpenCode: {machine}")
            return f"opencode-linux-{arch}.tar.gz"
        if system == "windows":
            if arch != "x64":
                raise RuntimeError(f"Unsupported Windows architecture for OpenCode: {machine}")
            return "opencode-windows-x64.zip"

        raise RuntimeError(f"Unsupported platform for OpenCode runtime install: {platform.system()}")

    @classmethod
    def _get_opencode_update_lock(cls) -> asyncio.Lock:
        lock = cls._opencode_update_lock
        if lock is None:
            lock = asyncio.Lock()
            cls._opencode_update_lock = lock
        return lock

    @classmethod
    def _get_opencode_server_lock(cls) -> asyncio.Lock:
        lock = cls._opencode_server_lock
        if lock is None:
            lock = asyncio.Lock()
            cls._opencode_server_lock = lock
        return lock

    @staticmethod
    def _normalize_release_tag(tag: str | None) -> str:
        val = (tag or "").strip().lower()
        if val.startswith("v"):
            val = val[1:]
        return val

    @classmethod
    def _should_install_opencode_release(
        cls,
        *,
        target_exists: bool,
        installed_tag: str | None,
        latest_tag: str | None,
    ) -> bool:
        if not target_exists:
            return True
        latest = cls._normalize_release_tag(latest_tag)
        if not latest:
            return False
        installed = cls._normalize_release_tag(installed_tag)
        if not installed:
            return True
        return installed != latest

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _opencode_update_check_interval_seconds(self) -> int:
        raw = (os.getenv("KYBER_OPENCODE_UPDATE_CHECK_INTERVAL_SECONDS", "") or "").strip()
        if raw:
            try:
                parsed = int(raw)
                if parsed >= 0:
                    return parsed
            except ValueError:
                pass
        return 6 * 60 * 60

    @staticmethod
    def _is_truthy_env(value: str | None) -> bool:
        val = (value or "").strip().lower()
        return val in {"1", "true", "yes", "on"}

    @staticmethod
    def _is_falsy_env(value: str | None) -> bool:
        val = (value or "").strip().lower()
        return val in {"0", "false", "no", "off"}

    def _fast_prompt_mode_enabled(self) -> bool:
        raw = os.getenv("KYBER_OPENCODE_FAST_MODE")
        if raw is None:
            # Default to lean prompts for native-feeling responsiveness.
            return True
        return self._is_truthy_env(raw)

    def _strict_opencode_isolation_enabled(self) -> bool:
        raw = os.getenv("KYBER_OPENCODE_STRICT_ISOLATION")
        if raw is None:
            # Default is native-ish behavior (project config/plugins allowed).
            return False
        if self._is_falsy_env(raw):
            return False
        return self._is_truthy_env(raw)

    def _use_runtime_home(self) -> bool:
        raw = os.getenv("KYBER_OPENCODE_FORCE_RUNTIME_HOME")
        if raw is None:
            # Native OpenCode behavior: use user's normal HOME unless strict
            # isolation is explicitly enabled.
            return self._strict_opencode_isolation_enabled()
        return self._is_truthy_env(raw)

    def _opencode_attach_server_enabled(self) -> bool:
        raw = os.getenv("KYBER_OPENCODE_ATTACH_SERVER")
        if raw is None:
            # Default on for "native OpenCode" responsiveness.
            return True
        return self._is_truthy_env(raw)

    def _opencode_question_tool_enabled(self) -> bool:
        raw = os.getenv("KYBER_OPENCODE_ALLOW_QUESTION_TOOL")
        if raw is None:
            return True
        return self._is_truthy_env(raw)

    def _opencode_server_port(self) -> int:
        raw = (os.getenv("KYBER_OPENCODE_SERVER_PORT", "") or "").strip()
        if raw:
            try:
                val = int(raw)
                if 1 <= val <= 65535:
                    return val
            except ValueError:
                pass
        return 4096

    def _opencode_session_key(self) -> str:
        channel = str(getattr(self.task, "origin_channel", "task") or "task")
        chat_id = str(getattr(self.task, "origin_chat_id", "direct") or "direct")
        raw = f"kyber:{channel}:{chat_id}"
        out = re.sub(r"[^A-Za-z0-9._:-]+", "-", raw).strip("-")
        return out[:120] if out else f"kyber:{self.task.id}"

    def _opencode_session_id(self) -> str | None:
        # OpenCode session continuity is removed in DeepAgents mode.
        return None

    @staticmethod
    def _codesearch_profile_key(provider_name: str, model_id: str, base_url: str | None) -> str:
        p = (provider_name or "unknown").strip().lower()
        m = (model_id or "unknown").strip().lower()
        b = (base_url or "").strip().lower()
        return f"{p}|{m}|{b}"

    @staticmethod
    def _is_generic_codesearch_profile(key: str) -> bool:
        return key.startswith("unknown|") or "|unknown|" in key

    def _codesearch_runtime_profile_key(self) -> str:
        provider = getattr(self, "provider", None)
        provider_name = (getattr(provider, "provider_name", None) or "unknown").strip().lower()
        model = (
            getattr(self, "model", None)
            or (provider.get_default_model() if provider and hasattr(provider, "get_default_model") else "")
            or "unknown"
        )
        base_url = getattr(provider, "api_base", None)
        if not base_url:
            with contextlib.suppress(Exception):
                base_url = self._resolve_task_base_url()
        return self._codesearch_profile_key(provider_name, model, base_url)

    def _disable_codesearch_by_default(self) -> bool:
        key = self._codesearch_runtime_profile_key()
        if key in self._codesearch_disabled_profiles and not self._is_generic_codesearch_profile(key):
            return True

        raw_all = os.getenv("KYBER_OPENCODE_DISABLE_CODESEARCH_BY_DEFAULT")
        if raw_all is not None and self._is_truthy_env(raw_all):
            return True

        provider = getattr(self, "provider", None)
        provider_name = (getattr(provider, "provider_name", None) or "").strip().lower()
        base_url = (getattr(provider, "api_base", None) or "").strip().lower()
        if provider_name in {"z.ai", "zai"}:
            return True
        if "api.z.ai" in base_url:
            return True

        raw_list = (os.getenv("KYBER_OPENCODE_DISABLE_CODESEARCH_PROVIDERS", "") or "").strip().lower()
        if raw_list:
            blocked = {p.strip() for p in raw_list.split(",") if p.strip()}
            if provider_name and provider_name in blocked:
                return True

        return False

    def _should_check_opencode_update(self, last_checked_at: float | None, *, now: float | None = None) -> bool:
        interval = self._opencode_update_check_interval_seconds()
        if interval == 0:
            return True
        if last_checked_at is None:
            return True
        if now is None:
            now = time.time()
        return (now - last_checked_at) >= interval

    def _read_opencode_manifest(self) -> dict[str, Any]:
        path = self._managed_opencode_manifest_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_opencode_manifest(self, manifest: dict[str, Any]) -> None:
        path = self._managed_opencode_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)

    def _fetch_latest_opencode_release(self) -> dict[str, str]:
        api_url = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "kyber-worker",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            release = json.loads(resp.read().decode("utf-8"))

        wanted = self._opencode_release_asset_name()
        assets = release.get("assets") or []
        asset = next((a for a in assets if a.get("name") == wanted), None)
        if not asset or not asset.get("browser_download_url"):
            raise RuntimeError(f"OpenCode release asset not found: {wanted}")

        tag = str(release.get("tag_name") or "").strip()
        if not tag:
            raise RuntimeError("OpenCode release metadata missing tag_name")

        url = str(asset["browser_download_url"]).strip()
        if not url:
            raise RuntimeError("OpenCode release metadata missing browser_download_url")

        return {
            "tag": tag,
            "asset_name": wanted,
            "download_url": url,
        }

    def _install_managed_opencode(self, *, release: dict[str, str]) -> Path:
        """Download and install OpenCode binary into Kyber runtime dir."""
        target = self._managed_opencode_path()
        wanted = release.get("asset_name", "")
        url = release.get("download_url", "")
        if not wanted or not url:
            raise RuntimeError("Invalid OpenCode release metadata for install")

        with tempfile.TemporaryDirectory(prefix="kyber-opencode-install-") as td:
            tmp = Path(td)
            archive = tmp / wanted
            dreq = urllib.request.Request(url, headers={"User-Agent": "kyber-worker"})
            with urllib.request.urlopen(dreq, timeout=120) as resp:  # nosec B310
                archive.write_bytes(resp.read())

            extract_dir = tmp / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            if wanted.endswith(".tar.gz"):
                with tarfile.open(archive, "r:gz") as tf:
                    tf.extractall(extract_dir)
            elif wanted.endswith(".zip"):
                with zipfile.ZipFile(archive, "r") as zf:
                    zf.extractall(extract_dir)
            else:
                extract_dir = tmp

            exe_names = {"opencode.exe", "opencode"} if os.name == "nt" else {"opencode"}
            source: Path | None = None
            for p in extract_dir.rglob("*"):
                if p.is_file() and p.name in exe_names:
                    source = p
                    break

            if source is None and archive.is_file() and archive.name in exe_names:
                source = archive
            if source is None:
                raise RuntimeError("Downloaded OpenCode archive did not contain an opencode executable")

            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.parent / f"{target.name}.new"
            shutil.copy2(source, staged)
            if os.name != "nt":
                staged.chmod(0o755)
            staged.replace(target)
            if os.name != "nt":
                target.chmod(0o755)

        return target

    def _ensure_managed_opencode_sync(self) -> Path:
        target = self._managed_opencode_path()
        manifest = self._read_opencode_manifest()
        now = time.time()
        installed_tag = str(manifest.get("installed_tag") or "").strip()
        last_checked_at = self._safe_float(manifest.get("last_checked_at"))

        should_check_remote = (not target.exists()) or self._should_check_opencode_update(last_checked_at, now=now)
        if not should_check_remote and target.exists():
            return target

        try:
            release = self._fetch_latest_opencode_release()
            latest_tag = release.get("tag", "")
            if self._should_install_opencode_release(
                target_exists=target.exists(),
                installed_tag=installed_tag,
                latest_tag=latest_tag,
            ):
                logger.info(
                    f"Worker [{self.task.id}] updating managed OpenCode "
                    f"{installed_tag or '(unknown)'} -> {latest_tag}"
                )
                target = self._install_managed_opencode(release=release)
                installed_tag = latest_tag
            manifest["latest_tag"] = latest_tag
        except Exception as e:
            if target.exists():
                logger.warning(f"Worker [{self.task.id}] OpenCode update check failed; using installed binary: {e}")
                manifest["last_check_error"] = str(e)[:400]
                manifest["last_checked_at"] = now
                if installed_tag:
                    manifest["installed_tag"] = installed_tag
                with contextlib.suppress(Exception):
                    self._write_opencode_manifest(manifest)
                return target
            raise

        manifest["last_checked_at"] = now
        if installed_tag:
            manifest["installed_tag"] = installed_tag
        manifest.pop("last_check_error", None)
        self._write_opencode_manifest(manifest)
        return target

    async def _ensure_managed_opencode(self) -> Path:
        lock = self._get_opencode_update_lock()
        async with lock:
            return await asyncio.to_thread(self._ensure_managed_opencode_sync)

    async def _resolve_opencode_command_prefix(self) -> list[str]:
        """Resolve how to invoke OpenCode."""
        try:
            installed = await self._ensure_managed_opencode()
            if installed.exists():
                return [str(installed)]
        except Exception as e:
            logger.warning(f"Worker [{self.task.id}] managed OpenCode install failed, trying fallback: {e}")

        opencode_bin = shutil.which("opencode")
        if opencode_bin:
            return [opencode_bin]

        raise RuntimeError(
            "OpenCode runtime unavailable. Kyber failed to provision a managed OpenCode runtime and no "
            "system `opencode` binary is available."
        )

    def _sync_opencode_skills(self) -> None:
        """Mirror Kyber skills into workspace/.opencode/skills for native skill tool discovery."""
        from kyber.agent.skills import SkillsLoader

        loader = SkillsLoader(self.workspace)
        skills = loader.list_skills(filter_unavailable=False)
        target_root = self.workspace / ".opencode" / "skills"
        target_root.mkdir(parents=True, exist_ok=True)

        desired_names: set[str] = set()
        for item in skills:
            name = str(item.get("name") or "").strip()
            skill_md = str(item.get("path") or "").strip()
            if not name or not skill_md:
                continue
            src_dir = Path(skill_md).parent
            if not (src_dir / "SKILL.md").exists():
                continue
            desired_names.add(name)

            dest = target_root / name
            same_link = False
            if dest.exists() and dest.is_symlink():
                with contextlib.suppress(Exception):
                    same_link = dest.resolve() == src_dir.resolve()
            if same_link:
                continue

            if dest.exists() or dest.is_symlink():
                if dest.is_symlink() or dest.is_file():
                    with contextlib.suppress(Exception):
                        dest.unlink()
                elif dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)

            try:
                dest.symlink_to(src_dir, target_is_directory=True)
            except Exception:
                shutil.copytree(src_dir, dest, dirs_exist_ok=True)

        # Best-effort stale cleanup.
        for child in target_root.iterdir():
            if child.name in desired_names:
                continue
            if child.is_symlink() or child.is_file():
                with contextlib.suppress(Exception):
                    child.unlink()
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    @staticmethod
    def _extract_candidate_paths(text: str) -> list[str]:
        out: list[str] = []
        if not text:
            return out
        patterns = [
            r"`(/[^`\n]+)`",
            r"`([A-Za-z]:\\[^`\n]+)`",
            r"(?<![A-Za-z0-9_])(/(?:[^\s`'\"<>])+)",
            r"(?<![A-Za-z0-9_])([A-Za-z]:\\(?:[^\s`'\"<>])+)",
        ]
        for pat in patterns:
            for match in re.findall(pat, text):
                s = str(match).strip().strip(",.;:")
                if s and s not in out:
                    out.append(s)
        return out

    def _resolve_file_attachments(self, description: str) -> list[str]:
        """Resolve likely relevant files to attach directly to OpenCode runs."""
        raw_max = (os.getenv("KYBER_OPENCODE_MAX_ATTACH_FILES", "") or "").strip()
        max_files = 4
        if raw_max:
            with contextlib.suppress(ValueError):
                max_files = max(0, min(12, int(raw_max)))
        if max_files <= 0:
            return []

        files: list[str] = []
        for candidate in self._extract_candidate_paths(description):
            p = Path(candidate).expanduser()
            if not p.is_absolute():
                p = (self.workspace / p).resolve()
            if not p.exists() or not p.is_file():
                continue
            try:
                # Keep attachments focused and small.
                if p.stat().st_size > 256 * 1024:
                    continue
            except OSError:
                continue
            s = str(p)
            if s in files:
                continue
            files.append(s)
            if len(files) >= max_files:
                break
        return files

    async def _is_tcp_port_open(self, host: str, port: int, timeout: float = 0.5) -> bool:
        try:
            conn = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=timeout)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            _ = reader
            return True
        except Exception:
            return False

    async def _stop_opencode_server_locked(self) -> None:
        proc = self._opencode_server_process
        self._opencode_server_process = None
        self._opencode_server_signature = None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _ensure_opencode_server(
        self,
        *,
        command_prefix: list[str],
        env: dict[str, str],
        signature: str,
    ) -> str | None:
        """Ensure a hot OpenCode server exists for --attach runs."""
        if not self._opencode_attach_server_enabled():
            return None

        host = "127.0.0.1"
        port = self._opencode_server_port()
        attach_url = f"http://{host}:{port}"
        lock = self._get_opencode_server_lock()

        async with lock:
            # If a compatible server is already up, reuse it.
            if await self._is_tcp_port_open(host, port):
                proc = self._opencode_server_process
                if proc and proc.returncode is None and self._opencode_server_signature == signature:
                    return attach_url
                if proc is None:
                    # External/previous server already listening; best-effort reuse.
                    return attach_url
                # Wrong signature; restart below.
                await self._stop_opencode_server_locked()

            # If tracked process exists but port is down, clear it.
            proc = self._opencode_server_process
            if proc and proc.returncode is not None:
                self._opencode_server_process = None
                self._opencode_server_signature = None

            try:
                server_cmd = [
                    *command_prefix,
                    "serve",
                    "--hostname",
                    host,
                    "--port",
                    str(port),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *server_cmd,
                    cwd=str(self.workspace),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._opencode_server_process = proc
                self._opencode_server_signature = signature
            except Exception as e:
                logger.warning(f"Worker [{self.task.id}] could not start OpenCode server for --attach: {e}")
                return None

            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if proc.returncode is not None:
                    break
                if await self._is_tcp_port_open(host, port):
                    return attach_url
                await asyncio.sleep(0.2)

            await self._stop_opencode_server_locked()
            logger.warning(f"Worker [{self.task.id}] OpenCode server did not become ready; falling back to direct run")
            return None

    def _resolve_task_base_url(self) -> str:
        """Resolve OpenAI-compatible base URL for the configured task provider."""
        explicit = (self.provider.api_base or "").strip()
        if explicit:
            return explicit.rstrip("/")

        provider_name = (getattr(self.provider, "provider_name", None) or "").strip().lower()
        defaults = {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "anthropic": "https://api.anthropic.com/v1",
        }
        url = defaults.get(provider_name)
        if url:
            return url
        is_custom = getattr(self.provider, "is_custom", False)
        if is_custom:
            raise RuntimeError(
                f"Custom provider '{provider_name or '(unnamed)'}' has no api_base configured. "
                f"Set api_base in your provider config to the OpenAI-compatible endpoint URL."
            )
        return "https://api.openai.com/v1"

    def _build_opencode_config_content(
        self,
        *,
        provider_id: str,
        model_id: str,
        base_url: str | None,
        provider_name: str,
        env_key_name: str,
        force_openai_compatible: bool,
        allow_codesearch: bool = True,
        allow_read: bool = True,
        instruction_files: list[str] | None = None,
        include_api_key_env: bool = True,
    ) -> str:
        """Build isolated OpenCode config for Kyber worker execution."""
        provider_timeout: int | bool
        if self.exec_timeout <= 0:
            # Preserve "no timeout" intent for provider requests.
            provider_timeout = False
        else:
            # Provider round-trips can take significantly longer than shell tool
            # execution, so keep this generous to avoid spurious TimeoutError.
            provider_timeout = max(300_000, int(self.exec_timeout * 1000))

        provider_options: dict[str, Any] = {
            "timeout": provider_timeout,
        }
        if base_url:
            provider_options["baseURL"] = base_url
        if provider_name == "openrouter":
            provider_options["headers"] = {
                "HTTP-Referer": "https://kyber.chat",
                "X-Title": "Kyber",
            }

        provider_cfg: dict[str, Any] = {
            "name": "Kyber Task Provider",
            "options": provider_options,
            "models": {
                model_id: {
                    "id": model_id,
                    "name": model_id,
                    "tool_call": True,
                    "temperature": True,
                },
            },
        }
        if include_api_key_env:
            provider_cfg["env"] = [env_key_name]
        if base_url:
            provider_cfg["api"] = base_url
        if force_openai_compatible:
            provider_cfg["npm"] = "@ai-sdk/openai-compatible"

        config = {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": [provider_id],
            "model": f"{provider_id}/{model_id}",
            "permission": {
                "read": "allow" if allow_read else "deny",
                "edit": "allow",
                "list": "allow",
                "glob": "allow",
                "grep": "allow",
                "bash": "allow",
                "webfetch": "allow",
                "websearch": "allow",
                "codesearch": "allow" if allow_codesearch else "deny",
                "external_directory": "allow",
                "todowrite": "allow",
                "todoread": "allow",
                "question": "allow" if self._opencode_question_tool_enabled() else "deny",
                "task": "deny",
            },
            "provider": {
                provider_id: provider_cfg,
            },
        }
        if instruction_files:
            config["instructions"] = instruction_files
        return json.dumps(config, separators=(",", ":"))

    @classmethod
    def _is_codesearch_chunk_limit_error(cls, text: str) -> bool:
        lower = (text or "").lower()
        return any(marker in lower for marker in cls._CODESEARCH_CHUNK_ERROR_MARKERS)

    @staticmethod
    def _shorten(value: str, limit: int = 140) -> str:
        text = " ".join((value or "").split()).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _site_title_from_webfetch_args(args: dict[str, Any]) -> str:
        """Derive a short, non-link site title from webfetch args."""
        raw_title = " ".join(str(args.get("title", "")).split()).strip()
        if raw_title:
            return raw_title

        raw_url = str(args.get("url", "")).strip()
        if not raw_url:
            return ""

        parsed = urllib.parse.urlparse(raw_url)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return ""
        if host.startswith("www."):
            host = host[4:]

        host_parts = [p for p in host.split(".") if p]
        if len(host_parts) < 2:
            return host

        known_domains = {
            "github.com": "GitHub",
            "openai.com": "OpenAI",
            "youtube.com": "YouTube",
            "wikipedia.org": "Wikipedia",
            "x.com": "X",
            "twitter.com": "X",
            "reddit.com": "Reddit",
            "stackoverflow.com": "Stack Overflow",
            "docs.python.org": "Python Docs",
        }
        known = known_domains.get(host)
        if known:
            return known

        root_domain = ".".join(host_parts[-2:])
        root_label = known_domains.get(root_domain)
        if root_label is None:
            domain_token = host_parts[-2]
            words = [w for w in domain_token.replace("-", " ").replace("_", " ").split() if w]
            if not words:
                root_label = domain_token
            else:
                root_label = " ".join(w.upper() if len(w) <= 2 else w.capitalize() for w in words)

        subdomain_hints = {
            "docs": "Docs",
            "blog": "Blog",
            "developer": "Developer",
            "developers": "Developer",
            "help": "Help",
            "support": "Support",
            "status": "Status",
            "api": "API",
            "news": "News",
        }
        sub_parts = host_parts[:-2]
        hint = ""
        if sub_parts:
            candidate = sub_parts[-1].lower()
            hint = subdomain_hints.get(candidate, "")

        return f"{root_label} {hint}".strip()

    def _format_opencode_action(self, tool_name: str, args: dict[str, Any]) -> str:
        """Human-readable action for OpenCode tool events."""
        name = (tool_name or "").strip()
        args = args or {}

        if name == "bash":
            cmd = self._shorten(str(args.get("command", "")))
            return f"running `{cmd}`" if cmd else "running a shell command"
        if name == "read":
            p = self._shorten(str(args.get("filePath", "")))
            return f"reading `{p}`" if p else "reading a file"
        if name == "list":
            p = self._shorten(str(args.get("path", "")))
            return f"checking `{p}`" if p else "checking a folder"
        if name == "write":
            p = self._shorten(str(args.get("filePath", "")))
            return f"writing `{p}`" if p else "writing a file"
        if name == "edit":
            p = self._shorten(str(args.get("filePath", "")))
            return f"editing `{p}`" if p else "editing a file"
        if name == "websearch":
            q = self._shorten(str(args.get("query", "")))
            return f"searching for “{q}”" if q else "searching the web"
        if name == "webfetch":
            site_title = self._shorten(self._site_title_from_webfetch_args(args), limit=70)
            return f"opening {site_title}" if site_title else "opening a web page"
        if name == "glob":
            pat = self._shorten(str(args.get("pattern", "")))
            return f"globbing `{pat}`" if pat else "searching files"
        if name == "grep":
            pat = self._shorten(str(args.get("pattern", "")))
            return f"grepping `{pat}`" if pat else "searching content"
        return "making progress"

    @staticmethod
    def _extract_opencode_error(raw: Any) -> str | None:
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, dict):
                msg = data.get("message")
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
            for key in ("message", "name", "code"):
                val = raw.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return json.dumps(raw)
        if isinstance(raw, str):
            msg = raw.strip()
            return msg or None
        if raw is None:
            return None
        return str(raw).strip() or None

    async def _execute_with_opencode(self) -> str:
        """Run the task through OpenCode with targeted fallback for known chunk-limit failures."""
        fast_mode = self._fast_prompt_mode_enabled()
        disable_codesearch_first = self._disable_codesearch_by_default()
        attempts: list[dict[str, bool]]
        if disable_codesearch_first:
            attempts = [
                {"disable_codesearch": True, "compact_prompt": True, "disable_read": True},
            ]
        else:
            attempts = [
                {"disable_codesearch": False, "compact_prompt": fast_mode, "disable_read": False},
                {"disable_codesearch": True, "compact_prompt": True, "disable_read": True},
            ]
        last_error: Exception | None = None
        for i, attempt in enumerate(attempts):
            try:
                return await self._execute_with_opencode_attempt(
                    disable_codesearch=bool(attempt["disable_codesearch"]),
                    compact_prompt=bool(attempt["compact_prompt"]),
                    disable_read=bool(attempt["disable_read"]),
                )
            except Exception as e:
                last_error = e
                msg = str(e)
                is_chunk_error = self._is_codesearch_chunk_limit_error(msg)
                profile_key = self._codesearch_runtime_profile_key()
                if is_chunk_error and not self._is_generic_codesearch_profile(profile_key):
                    self._codesearch_disabled_profiles.add(profile_key)
                is_last = i == (len(attempts) - 1)
                if (not is_chunk_error) or is_last:
                    raise
                logger.warning(
                    f"Worker [{self.task.id}] retrying after chunk-limit failure "
                    f"(next attempt {i + 2}/{len(attempts)}): {msg}"
                )

        if last_error:
            raise last_error
        raise RuntimeError("OpenCode execution failed without an explicit error.")

    async def _execute_with_opencode_attempt(
        self,
        *,
        disable_codesearch: bool,
        compact_prompt: bool,
        disable_read: bool,
    ) -> str:
        """Run the task through OpenCode and return the final assistant message."""
        command_prefix = await self._resolve_opencode_command_prefix()
        provider_name = (getattr(self.provider, "provider_name", None) or "openai").strip().lower()
        raw_model = (self.model or self.provider.get_default_model() or "").strip()
        if not raw_model:
            raise RuntimeError("No task model configured for worker.")

        api_key = (self.provider.api_key or "").strip()

        provider_alias = {
            "gemini": "google",
            "z.ai": "zai",
        }
        canonical_provider = provider_alias.get(provider_name, provider_name)
        known_native = {"openai", "anthropic", "openrouter", "deepseek", "groq", "google", "zai"}
        is_custom_provider = bool(getattr(self.provider, "is_custom", False))
        force_openai_compatible = is_custom_provider or canonical_provider not in known_native
        if not api_key and force_openai_compatible:
            raise RuntimeError(f"No API key configured for task provider '{provider_name}'.")

        provider_id = "kyber_task" if force_openai_compatible else canonical_provider
        model_id = raw_model
        provider_prefix = f"{canonical_provider}/"
        if not force_openai_compatible and model_id.lower().startswith(provider_prefix):
            model_id = model_id[len(provider_prefix):]
        if canonical_provider == "openrouter" and model_id.lower().startswith("openrouter/"):
            model_id = model_id[len("openrouter/"):]

        base_url = self._resolve_task_base_url() if force_openai_compatible else (self.provider.api_base or None)
        model_selector = f"{provider_id}/{model_id}"

        with contextlib.suppress(Exception):
            self._sync_opencode_skills()

        env = os.environ.copy()
        env_key_name = "KYBER_OPENCODE_TASK_API_KEY"
        if api_key:
            env[env_key_name] = api_key
        else:
            env.pop(env_key_name, None)
        instruction_files = self._resolve_instruction_files()
        env["OPENCODE_CONFIG_CONTENT"] = self._build_opencode_config_content(
            provider_id=provider_id,
            model_id=model_id,
            base_url=base_url,
            provider_name=provider_name,
            env_key_name=env_key_name,
            force_openai_compatible=force_openai_compatible,
            allow_codesearch=not disable_codesearch,
            allow_read=not disable_read,
            instruction_files=instruction_files,
            include_api_key_env=bool(api_key),
        )
        # Prevent third-party user plugins/config from crashing worker runs.
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        if self._strict_opencode_isolation_enabled():
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
            env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
            env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        if self.exec_timeout > 0:
            env["OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"] = str(int(self.exec_timeout * 1000))
        else:
            # OpenCode's bash tool doesn't support true "infinite" timeout via
            # config; use a very large default to emulate no timeout.
            env["OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"] = str(24 * 60 * 60 * 1000)

        if self._use_runtime_home():
            runtime_home = self._opencode_runtime_home()
            runtime_home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(runtime_home)
            if os.name == "nt":
                env["USERPROFILE"] = str(runtime_home)

        server_signature = hashlib.sha256(
            "|".join([
                provider_id,
                model_id,
                str(base_url or ""),
                str(bool(force_openai_compatible)),
                str(bool(disable_codesearch)),
                str(bool(disable_read)),
                hashlib.sha256((api_key or "").encode("utf-8")).hexdigest(),
            ]).encode("utf-8")
        ).hexdigest()
        attach_url = await self._ensure_opencode_server(
            command_prefix=command_prefix,
            env=env,
            signature=server_signature,
        )

        prompt = self._build_worker_prompt_compact() if compact_prompt else self._build_worker_prompt()
        if disable_codesearch:
            prompt += (
                "\n\nTool availability note:\n"
                "- The codesearch tool is disabled for this run due to a known chunking error.\n"
                "- Use list/glob/grep/read/bash to locate and edit files.\n"
            )
        if disable_read:
            prompt += (
                "- The read tool is disabled for this run.\n"
                "- Use bash with `sed`, `head`, `tail`, `awk`, or `rg` for focused reads.\n"
            )
        attachments = self._resolve_file_attachments(self.task.description)
        base_session_key = self._opencode_session_key()
        resume_session_id = self._opencode_session_id()

        cmd: list[str] = [
            *command_prefix,
            "run",
            "--format",
            "json",
            "--model",
            model_selector,
        ]
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        if attach_url:
            cmd.extend(["--attach", attach_url])
        for file_path in attachments:
            cmd.extend(["--file", file_path])
        cmd.extend([
            prompt,
        ])

        logger.info(
            f"Worker [{self.task.id}] starting OpenCode engine | provider={provider_name} | "
            f"model={model_id} | codesearch={'off' if disable_codesearch else 'on'} | "
            f"read={'off' if disable_read else 'on'} | "
            f"prompt={'compact' if compact_prompt else 'full'} | "
            f"attach={'on' if attach_url else 'off'} | files={len(attachments)}"
        )
        boot_action = "starting OpenCode engine"
        self.registry.update_progress(self.task.id, current_action=boot_action)
        if self.narrator:
            self.narrator.narrate(self.task.id, boot_action)

        last_agent_message = ""
        seen_session_id = ""
        event_errors: list[str] = []
        iteration = 0
        last_activity = [time.monotonic()]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.workspace),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _consume_stdout() -> None:
            nonlocal last_agent_message, iteration, seen_session_id
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = str(evt.get("type", "")).strip()
                sid = str(evt.get("sessionID") or "").strip()
                if sid.startswith("ses"):
                    seen_session_id = sid
                if etype == "step_start":
                    iteration += 1
                    last_activity[0] = time.monotonic()
                    self.registry.update_progress(self.task.id, iteration=iteration)
                    continue

                if etype == "tool_use":
                    part = evt.get("part") or {}
                    if not isinstance(part, dict):
                        continue
                    state = part.get("state") or {}
                    args = state.get("input") if isinstance(state, dict) else {}
                    if not isinstance(args, dict):
                        args = {}
                    tool_name = str(part.get("tool", "")).strip()
                    action = self._format_opencode_action(tool_name, args)
                    last_activity[0] = time.monotonic()
                    if self.narrator:
                        self.narrator.narrate(self.task.id, action)
                    self.registry.update_progress(self.task.id, current_action=action)
                    self.registry.update_progress(self.task.id, current_action="", action_completed=action)

                    if tool_name == "bash" and isinstance(state, dict):
                        output = str(state.get("output") or "").strip()
                        if "Exit code:" in output and "Exit code: 0" not in output:
                            preview = output[:280] + ("…" if len(output) > 280 else "")
                            if preview:
                                event_errors.append(preview)
                    continue

                if etype == "text":
                    part = evt.get("part") or {}
                    if isinstance(part, dict):
                        text = str(part.get("text") or "").strip()
                        if text:
                            last_agent_message = text
                            last_activity[0] = time.monotonic()
                    continue

                if etype == "error":
                    msg = self._extract_opencode_error(evt.get("error"))
                    if msg:
                        event_errors.append(msg)
                        last_activity[0] = time.monotonic()
                    continue

        async def _consume_stderr() -> str:
            assert process.stderr is not None
            chunks: list[str] = []
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                txt = raw.decode("utf-8", errors="replace")
                if txt:
                    chunks.append(txt)
            return "".join(chunks)

        stderr_task = asyncio.create_task(_consume_stderr())
        stdout_task = asyncio.create_task(_consume_stdout())
        heartbeat_interval = 20.0
        raw_heartbeat = (os.getenv("KYBER_OPENCODE_HEARTBEAT_SECONDS", "") or "").strip()
        if raw_heartbeat:
            try:
                heartbeat_interval = max(5.0, float(raw_heartbeat))
            except ValueError:
                heartbeat_interval = 20.0

        async def _heartbeat() -> None:
            if heartbeat_interval <= 0:
                return
            while process.returncode is None:
                await asyncio.sleep(min(5.0, heartbeat_interval))
                if process.returncode is not None:
                    break
                idle_for = time.monotonic() - last_activity[0]
                if idle_for < heartbeat_interval:
                    continue
                action = "still working…"
                self.registry.update_progress(self.task.id, current_action=action)
                if self.narrator:
                    self.narrator.narrate(self.task.id, action)
                last_activity[0] = time.monotonic()

        heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            await stdout_task
            rc = await process.wait()
            stderr_output = await stderr_task
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            raise
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

        if rc != 0:
            stderr_preview = (stderr_output or "").strip()
            if len(stderr_preview) > 600:
                stderr_preview = stderr_preview[:600] + "…"
            err_chunks = [e for e in event_errors if e]
            if stderr_preview:
                err_chunks.append(stderr_preview)
            if not err_chunks:
                err_chunks.append(f"OpenCode exited with status {rc}")
            raise RuntimeError(" | ".join(err_chunks[:3]))

        _ = (base_session_key, seen_session_id)

        if not last_agent_message:
            if event_errors:
                raise RuntimeError(event_errors[-1])
            raise RuntimeError("OpenCode completed without a final message.")

        return last_agent_message


class WorkerPool:
    """
    Manages concurrent worker execution.

    Spawns workers as async tasks, tracks them, and ensures
    completion notifications are delivered.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        registry: TaskRegistry,
        persona_prompt: str,
        model: str | None = None,
        brave_api_key: str | None = None,
        max_concurrent: int = 5,
        timezone: str | None = None,
        exec_timeout: int = 60,
        narrator: LiveNarrator | None = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.registry = registry
        self.persona_prompt = persona_prompt
        self.model = model
        self.brave_api_key = brave_api_key
        self.max_concurrent = max_concurrent
        self.timezone = timezone
        self.exec_timeout = exec_timeout
        self.narrator = narrator

        # Shared workspace index — built once, reused across all workers
        self.workspace_index = WorkspaceIndex(workspace)

        self.completion_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._running: dict[str, asyncio.Task] = {}

    def spawn(self, task: Task) -> None:
        """Spawn a worker for the task."""
        # Register with narrator for live updates
        if self.narrator:
            self.narrator.register_task(
                task.id, task.origin_channel, task.origin_chat_id, task.label,
            )

        worker = Worker(
            task=task,
            provider=self.provider,
            workspace=self.workspace,
            registry=self.registry,
            completion_queue=self.completion_queue,
            persona_prompt=self.persona_prompt,
            model=self.model,
            brave_api_key=self.brave_api_key,
            exec_timeout=self.exec_timeout,
            timezone=self.timezone,
            workspace_index=self.workspace_index,
            narrator=self.narrator,
        )

        async_task = asyncio.create_task(worker.run())
        self._running[task.id] = async_task

        # Cleanup when done
        def _on_done(_: asyncio.Task) -> None:
            self._running.pop(task.id, None)
            if self.narrator:
                self.narrator.unregister_task(task.id)

        async_task.add_done_callback(_on_done)
        logger.info(f"Spawned worker for task: {task.label} [{task.id}]")

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task by task id."""
        t = self._running.get(task_id)
        if not t:
            return False
        t.cancel()
        return True

    async def run_inline(self, task: Task) -> Task:
        """
        Run a task to completion in the current event loop.

        This is used for "direct"/one-shot invocations where background tasks
        would be cancelled when the process exits (e.g., `kyber agent -m ...`).
        """
        # Use a throwaway queue; Worker.run() always puts the task on completion.
        q: asyncio.Queue[Task] = asyncio.Queue()
        worker = Worker(
            task=task,
            provider=self.provider,
            workspace=self.workspace,
            registry=self.registry,
            completion_queue=q,
            persona_prompt=self.persona_prompt,
            model=self.model,
            brave_api_key=self.brave_api_key,
            exec_timeout=self.exec_timeout,
            timezone=self.timezone,
            workspace_index=self.workspace_index,
            narrator=self.narrator,
        )
        if self.narrator:
            self.narrator.register_task(task.id, task.origin_channel, task.origin_chat_id, task.label)
        try:
            await worker.run()
        finally:
            if self.narrator:
                self.narrator.unregister_task(task.id)
        return task

    @property
    def active_count(self) -> int:
        """Number of currently running workers."""
        return len(self._running)

    async def get_completion(self) -> Task:
        """Wait for and return the next completed task."""
        return await self.completion_queue.get()

    def get_completion_nowait(self) -> Task | None:
        """Get a completed task if available, else None."""
        try:
            return self.completion_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
