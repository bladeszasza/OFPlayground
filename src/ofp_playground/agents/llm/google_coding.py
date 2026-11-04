# src/ofp_playground/agents/llm/google_coding.py
"""GoogleCodingAgent — native ToolCodeExecution via google.genai with file I/O."""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ofp_playground.agents.llm.codex import BaseCodingAgent, save_code_blocks
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL_GOOGLE = "gemini-3-flash-preview"


class GoogleCodingAgent(BaseCodingAgent):
    """Coding agent using Google Gemini's native ToolCodeExecution.

    Runs synchronously in a thread executor (matches GoogleAgent pattern).
    Supports file tools (list_workspace, read_file, write_file, edit_file)
    alongside native code execution. Code blocks in the output text are
    also extracted and saved as files.
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
            model=model or DEFAULT_MODEL_GOOGLE,
        )

    def _build_google_file_tools(self):
        """Convert Anthropic-format file tool schemas to Google FunctionDeclarations."""
        from google.genai import types

        declarations = []
        for t in self._file_tool_schemas():
            schema = t["input_schema"]
            props = {}
            for pname, pdef in schema.get("properties", {}).items():
                props[pname] = types.Schema(
                    type=types.Type.STRING,
                    description=pdef.get("description", ""),
                )
            declarations.append(
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties=props,
                        required=schema.get("required", []),
                    ),
                )
            )
        return declarations

    async def _run_code_loop(self, context: str) -> tuple[str, list[Path]]:
        import asyncio
        loop = asyncio.get_running_loop()
        await self._send_progress("Sending to Gemini code execution...")
        tools_disabled = self._tools_disabled_for_directive()

        def _call() -> tuple[str, list[Path]]:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self._api_key)

            # Build tools: native code execution + file tools
            tool_list: list[types.Tool | Callable] = []
            if not tools_disabled:
                tool_list.append(types.Tool(code_execution=types.ToolCodeExecution()))
                file_decls = self._build_google_file_tools()
                if file_decls:
                    tool_list.append(types.Tool(function_declarations=file_decls))

            config = types.GenerateContentConfig(
                system_instruction=self._synopsis,
                tools=tool_list or None,
                max_output_tokens=16000,
            )

            # Multi-turn loop for function calls
            contents: list = [context]
            output_parts: list[str] = []

            for _round in range(25):
                response = client.models.generate_content(
                    model=self._model or DEFAULT_MODEL_GOOGLE,
                    contents=contents,
                    config=config,
                )

                candidates = getattr(response, "candidates", None) or []
                if not candidates:
                    break

                parts = getattr(candidates[0].content, "parts", None) or []
                fn_calls = []

                for part in parts:
                    text = getattr(part, "text", "") or ""
                    if text:
                        output_parts.append(text)
                    exec_result = getattr(part, "code_execution_result", None)
                    if exec_result:
                        stdout = getattr(exec_result, "output", "") or ""
                        if stdout:
                            output_parts.append(stdout)
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", "") in self._FILE_TOOL_NAMES:
                        fn_calls.append(fc)

                if not fn_calls:
                    break

                # Build function responses and continue
                contents.append(candidates[0].content)
                fn_response_parts = []
                for fc in fn_calls:
                    args = dict(fc.args or {}) if fc.args else {}
                    result = self._execute_file_tool(fc.name, args)
                    fn_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"result": result},
                        )
                    )
                contents.append(types.Content(parts=fn_response_parts, role="user"))

            output_text = "\n".join(filter(None, output_parts))
            saved = save_code_blocks(output_text, self._output_dir, self._name)
            return output_text, saved

        return await loop.run_in_executor(None, _call)
