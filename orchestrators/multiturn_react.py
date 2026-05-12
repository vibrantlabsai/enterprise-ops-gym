"""Multi-turn ReAct orchestrator with a user simulator.

Replaces single-turn ReAct's termination rule (``no tool calls = done``)
with a conversational one: any plain-text portion of the agent's response
is routed to the user simulator, and the loop continues until the user
emits ``##STOP##`` (or the simulator's turn budget is exhausted).

There is no synthetic ``send_message_to_user`` tool — the agent just
speaks, and the orchestrator delivers. Tool calls and user-facing text
can coexist in a single response: tool calls execute first (Bedrock
requires tool_use → tool_result adjacency), then the text (if any) is
routed to the simulator.
"""

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from user_simulator.simulator import UserSimulator
from .react import ReactOrchestrator

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_USER_INTERACTION_SUFFIX = (
    "\n\n## MULTI-TURN MODE — overrides earlier rules where they conflict\n"
    "A real user is on the other end of this conversation. The earlier rule "
    "*Do not ask for further information* is suspended in this mode and is "
    "REPLACED by the rules below.\n\n"
    "- Any plain-text content you produce IS delivered to the user. Write it "
    "in first person, addressed to them — not as internal narration. If you "
    "need a missing detail, just ask the user directly in your text.\n"
    "- Tool calls are executed against the underlying systems. You may emit "
    "text and tool calls in the same turn; both happen.\n"
    "- The user will signal when they consider the task complete. Until then, "
    "keep working — don't end with a passive summary if real actions are "
    "still required.\n"
)


def _extract_text(content: Any) -> str:
    """Pull text out of a LangChain/Bedrock response ``content`` field.

    Bedrock Converse returns either a plain string or a list of content
    blocks like ``[{"type": "text", "text": "..."}, {"type": "tool_use", ...}]``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", "") or "")
        return "".join(parts)
    return ""


class MultiTurnReactOrchestrator(ReactOrchestrator):
    """ReAct orchestrator with a user simulator on the other end."""

    def __init__(self, *args, user_simulator: UserSimulator, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_simulator = user_simulator

        # Augment the agent's system prompt so it knows there's a user on the
        # other end. Idempotent across reconstruction.
        if "MULTI-TURN MODE" not in (self.config.system_prompt or ""):
            self.config.system_prompt = (
                (self.config.system_prompt or "") + SYSTEM_PROMPT_USER_INTERACTION_SUFFIX
            )

    async def execute(self) -> Dict[str, Any]:
        """Run the loop. Termination: user-sim STOP, sim budget exceeded,
        defensive empty-response, or ``max_iterations``."""
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
        tools_used: List[str] = []
        tool_results: List[Dict[str, Any]] = []

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

            # 1) Execute tool calls first. Bedrock requires every tool_use
            #    block to be immediately followed by its tool_result.
            for tool_call in response.tool_calls or []:
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

            # 2) Route any plain-text content to the user simulator.
            text = _extract_text(response.content)
            if text.strip():
                sim_result = await self.user_simulator.respond(text)
                if sim_result.get("error"):
                    logger.error(
                        f"User simulator returned error; ending conversation: "
                        f"{sim_result['error']}"
                    )
                    break

                user_reply = sim_result["reply"]
                messages.append(HumanMessage(content=user_reply))
                conversation_flow.append(
                    {
                        "type": "user_message",
                        "content": user_reply,
                        "source": "user_simulator",
                        "stop_emitted": sim_result["stop"],
                        "budget_exceeded": sim_result["budget_exceeded"],
                    }
                )

                if sim_result["stop"]:
                    logger.info("User simulator emitted ##STOP##; ending conversation.")
                    break
                if sim_result["budget_exceeded"]:
                    logger.info("User simulator turn budget exceeded; ending conversation.")
                    break
            elif not response.tool_calls:
                # No text, no tools — defensive exit. Should not happen with
                # Bedrock, but if it does we'd loop forever otherwise.
                logger.info(
                    "LLM response had neither text nor tool calls; ending conversation."
                )
                break

        return {
            "final_response": messages[-1].content if messages else "",
            "conversation_flow": conversation_flow,
            "tools_used": tools_used,
            "tool_results": tool_results,
            "messages": messages,
        }

    def get_result_metadata(self) -> Dict[str, Any]:
        return {
            "multiturn": True,
            "user_simulator_calls": self.user_simulator.turn_count,
            "user_stop_emitted": self.user_simulator.stop_emitted,
        }
