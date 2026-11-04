# src/ofp_playground/agents/llm/anthropic_coding.py
"""AnthropicCodingAgent — native code_execution_20250825 beta tool with file I/O."""
from __future__ import annotations

import logging
from pathlib import Path

from ofp_playground.agents.llm.codex import BaseCodingAgent, save_code_blocks
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ANTHROPIC = "claude-opus-4-6"


class AnthropicCodingAgent(BaseCodingAgent):
    """Coding agent using Anthropic's native code_execution_20250825 beta tool.

    Supports multi-turn tool execution: the model can use both server-side
    code_execution (results come back inline) and client-side file tools
    (list_workspace, read_file, write_file, edit_file) against ``_output_dir``.

    Code blocks in the final output are also extracted and saved as files
    (fallback for when the model produces code inline instead of using tools).
    """

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str | None = None,
    ):
        super().__init__(
            name=name,
            synopsis=synopsis,
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model or DEFAULT_MODEL_ANTHROPIC,
        )

    async def _run_code_loop(self, context: str) -> tuple[str, list[Path]]:
        import anthropic

        await self._send_progress("Sending to Anthropic code_execution...")

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        tools_disabled = self._tools_disabled_for_directive()

        all_tools: list = []
        if not tools_disabled:
            all_tools = [
                {"type": "code_execution_20250825", "name": "code_execution"},
                *self._file_tool_schemas(),
            ]

        messages: list[dict] = [{"role": "user", "content": context}]
        response_content: list = []

        try:
            for _round in range(25):
                kwargs: dict = {
                    "model": self._model,
                    "max_tokens": 16000,
                    "system": self._synopsis,
                    "messages": messages,
                    "betas": ["code-execution-2025-05-22"],
                }
                if all_tools:
                    kwargs["tools"] = all_tools

                response = await client.beta.messages.create(**kwargs)
                response_content = list(response.content)

                # Check for file-tool calls needing client-side execution
                file_calls = [
                    b for b in response_content
                    if getattr(b, "type", "") == "tool_use"
                    and getattr(b, "name", "") in self._FILE_TOOL_NAMES
                ]

                if not file_calls or response.stop_reason == "end_turn":
                    break

                # Continue conversation with tool results
                messages.append({"role": "assistant", "content": response_content})
                tool_results = []
                for tc in file_calls:
                    result = self._execute_file_tool(tc.name, tc.input)
                    await self._send_progress(f"[{tc.name}] {result[:80]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })
                messages.append({"role": "user", "content": tool_results})
        finally:
            await client.close()

        # Extract output text
        output_parts: list[str] = []
        for block in response_content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text = getattr(block, "text", "")
                if text:
                    output_parts.append(text)
            elif block_type == "bash_code_execution_tool_result":
                for content_block in (getattr(block, "content", None) or []):
                    cb_type = getattr(content_block, "type", "")
                    if cb_type == "text":
                        stdout = getattr(content_block, "text", "")
                        if stdout:
                            output_parts.append(stdout)

        output_text = "\n".join(filter(None, output_parts))
        saved = save_code_blocks(output_text, self._output_dir, self._name)
        return output_text, saved
