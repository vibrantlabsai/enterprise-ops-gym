"""Multi-turn ReAct orchestrator with a user simulator.

Extends :class:`ReactOrchestrator` with two surgical changes:
- The opening user message is ``scenario.reason_for_call`` (from the
  user simulator), not the god's-eye ``user_prompt``.
- A synthetic ``send_message_to_user`` tool is exposed; calls to it are
  routed to the user simulator instead of an MCP server.

The base ReAct loop (no-tool-call termination, max_iterations,
message-history accumulation) is inherited unchanged.
"""

import json
import logging
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from user_simulator.simulator import UserSimulator
from .react import ReactOrchestrator

logger = logging.getLogger(__name__)


SEND_MESSAGE_TO_USER_TOOL_NAME = "send_message_to_user"


SEND_MESSAGE_TO_USER_TOOL = {
    "name": SEND_MESSAGE_TO_USER_TOOL_NAME,
    "description": (
        "Send a message to the user and receive their reply. Use this to ask "
        "clarifying questions, request missing information, or confirm task "
        "completion before ending the conversation. The reply field contains "
        "the user's response. If stop_emitted=true, the user has indicated the "
        "task is complete (or wants to stop) — you should typically finish up "
        "and end your turn shortly after."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "What you want to say to the user.",
            }
        },
        "required": ["message"],
    },
    "_synthetic": True,
}


SYSTEM_PROMPT_USER_INTERACTION_SUFFIX = (
    "\n\n## MULTI-TURN MODE — overrides earlier rules where they conflict\n"
    "A real user is on the other end of this conversation. The earlier rule "
    "*Do not ask for further information* is suspended in this mode and is "
    "REPLACED by the rules below.\n\n"
    f"- Use the `{SEND_MESSAGE_TO_USER_TOOL_NAME}` tool whenever a required detail "
    "is missing or ambiguous. The user only volunteers information when asked.\n"
    "- You MUST make at least one tool call on every turn until the task is fully "
    "complete. Never end a turn with a plain-text response that says what you "
    "*will* do — actually call the tool. If you need information, call "
    f"`{SEND_MESSAGE_TO_USER_TOOL_NAME}`. If you have enough information, call the "
    "appropriate domain tool.\n"
    f"- End the conversation only after every step is verifiably done and the "
    "user has confirmed (or after `stop_emitted=true` is returned by the user).\n"
)


class MultiTurnReactOrchestrator(ReactOrchestrator):
    """ReAct orchestrator with a user simulator on the other end."""

    def __init__(self, *args, user_simulator: UserSimulator, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_simulator = user_simulator

        # Ensure the synthetic user-talk tool is registered on the tools array.
        # Idempotent — execute_single_run constructs a new orchestrator per run
        # against the same available_tools list, so guard against duplicates.
        if not any(
            t.get("name") == SEND_MESSAGE_TO_USER_TOOL_NAME
            for t in self.available_tools
        ):
            self.available_tools.append(dict(SEND_MESSAGE_TO_USER_TOOL))

        # Augment the agent's system prompt so it knows the tool exists and how
        # to use it. Idempotent across reconstruction.
        if SEND_MESSAGE_TO_USER_TOOL_NAME not in (self.config.system_prompt or ""):
            self.config.system_prompt = (
                (self.config.system_prompt or "") + SYSTEM_PROMPT_USER_INTERACTION_SUFFIX
            )

    async def execute(self) -> Dict[str, Any]:
        """Run the ReAct loop with the user simulator opening the conversation."""
        opener = await self.user_simulator.opening_message()

        messages = [
            SystemMessage(content=self.config.system_prompt),
            HumanMessage(content=opener),
        ]

        conversation_flow = [
            {"type": "system_message", "content": self.config.system_prompt},
            {
                "type": "user_message",
                "content": opener,
                "source": "user_simulator",
            },
        ]
        tools_used = []
        tool_results = []

        for iteration in range(self.max_iterations):
            logger.info(f"\n--- Iteration {iteration + 1} ---")

            response = await self.llm_client.invoke_with_tools(
                messages, self.available_tools
            )
            reasoning_details = (getattr(response, "additional_kwargs", None) or {}).get(
                "reasoning_details"
            )
            if reasoning_details:
                logger.debug(
                    f"Preserving reasoning_details for next turn ({len(reasoning_details)} items)"
                )

            messages.append(response)

            usage_metadata = (
                response.usage_metadata if hasattr(response, "usage_metadata") else {}
            )
            response_metadata = (
                response.response_metadata
                if hasattr(response, "response_metadata")
                else {}
            )

            conversation_flow.append(
                {
                    "type": "ai_message",
                    "content": response.content,
                    "usage_metadata": usage_metadata,
                    "response_metadata": response_metadata,
                    "tool_calls": [
                        {"name": tc["name"], "args": tc["args"]}
                        for tc in (response.tool_calls or [])
                    ],
                }
            )

            logger.info(f"LLM Response: {response.content}")

            if not response.tool_calls or len(response.tool_calls) == 0:
                logger.info("No tool calls requested. Task complete.")
                break

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                logger.debug(f"Tool arguments: {tool_args}")

                exec_result = await self._execute_tool_call(tool_name, tool_args)
                tool_result = exec_result["result"]
                target_gym = exec_result["gym_server"]

                logger.info(f"Tool result success: {tool_result.get('success')}")

                if tool_name not in tools_used:
                    tools_used.append(tool_name)

                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        "result": tool_result,
                        "gym_server": target_gym,
                    }
                )

                messages.append(
                    ToolMessage(
                        content=json.dumps(tool_result.get("result", {})),
                        tool_call_id=tool_call.get("id", ""),
                    )
                )

                conversation_flow.append(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": tool_result,
                        "gym_server": target_gym,
                    }
                )

        return {
            "final_response": messages[-1].content if messages else "",
            "conversation_flow": conversation_flow,
            "tools_used": tools_used,
            "tool_results": tool_results,
            "messages": messages,
        }

    async def _execute_tool_call(
        self, tool_name: str, tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Route ``send_message_to_user`` to the user simulator; otherwise
        delegate to the MCP-routing logic in the base class."""
        if tool_name == SEND_MESSAGE_TO_USER_TOOL_NAME:
            agent_msg = tool_args.get("message", "") or ""
            sim_result = await self.user_simulator.respond(agent_msg)
            success = sim_result.get("error") is None
            wrapped = {
                "success": success,
                "result": {
                    "reply": sim_result["reply"],
                    "stop_emitted": sim_result["stop"],
                    "budget_exceeded": sim_result["budget_exceeded"],
                },
            }
            if not success:
                wrapped["error"] = sim_result["error"]
            return {"result": wrapped, "gym_server": "user_simulator"}

        return await super()._execute_tool_call(tool_name, tool_args)

    def get_result_metadata(self) -> Dict[str, Any]:
        return {
            "multiturn": True,
            "user_simulator_calls": self.user_simulator.turn_count,
            "user_stop_emitted": self.user_simulator.stop_emitted,
        }
