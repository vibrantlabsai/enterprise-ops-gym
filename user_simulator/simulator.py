"""User simulator that wraps a second LLM to play the user side of an
agent conversation.

Modeled after tau2-bench's HalfDuplexUser, but simpler:
- The user has no tools (no parallel call surface).
- No persona layer in v1.
- Stop signaling is advisory: the simulator emits `##STOP##` at the end of
  a reply when it considers the task complete; the orchestrator records this
  but agent-driven termination still owns the loop.
"""

import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from benchmark.llm_client import LLMClient
from user_simulator.prompts import build_user_simulator_prompt

logger = logging.getLogger(__name__)


STOP_SENTINEL = "###STOP###"
OUT_OF_SCOPE_SENTINEL = "###OUT-OF-SCOPE###"


class UserSimulator:
    """Plays the user role in a multi-turn agent evaluation.

    The simulator owns its own message history and is invoked once per
    agent->user round-trip via :meth:`respond`. From the simulator LLM's
    point of view, IT is the assistant — so messages from the agent arrive
    as ``HumanMessage`` and the simulator's own outputs are ``AIMessage``
    (the same role flip used by tau2's UserState).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        scenario: Dict[str, str],
        max_user_turns: int = 20,
    ):
        self.llm_client = llm_client
        self.scenario = scenario
        self.max_user_turns = max_user_turns
        self.turn_count = 0
        self.stop_emitted = False
        self.out_of_scope_emitted = False
        self.system_prompt = build_user_simulator_prompt(scenario)
        self.messages: List[Any] = [SystemMessage(content=self.system_prompt)]
        self._opened = False
        self._cached_opener: str = ""

    async def opening_message(self) -> str:
        """Generate the user's kickoff utterance via the simulator LLM.

        The LLM is given the scenario in its system prompt — where
        ``reason_for_call`` is framed as the user's motivation, not a script —
        and asked to phrase the opening naturally in first person. Falls back
        to the literal ``reason_for_call`` string only if the LLM call fails
        or returns empty (true safety net).

        Idempotent: subsequent calls return the cached opener and do not
        re-append to the message history.
        """
        if self._opened:
            return self._cached_opener

        elicit = HumanMessage(content=(
            "It is the start of the conversation — the agent has just become "
            "available. Speak FIRST as the user. Phrase your motivation "
            "naturally in first person ('I', 'my'), like a real person "
            "opening a chat or call with support. One or two short sentences "
            "is usually right. Paraphrase your motivation in your own words — "
            "do NOT quote the scenario verbatim. Do NOT list everything you "
            "know upfront; only say what's prompting you to reach out. Do "
            "not include `###STOP###` in this opening turn."
        ))

        text: str = ""
        try:
            response = await self.llm_client.llm.ainvoke(self.messages + [elicit])
            text = (getattr(response, "content", "") or "").strip()
            text = text.replace(STOP_SENTINEL, "").replace(OUT_OF_SCOPE_SENTINEL, "").strip()
        except Exception:
            logger.exception(
                "User simulator opener generation failed; falling back to "
                "literal reason_for_call"
            )

        if not text:
            text = self.scenario.get("reason_for_call") or ""

        # Seed running history with the opener (NOT the elicitor — that was a
        # one-shot meta-instruction that doesn't belong in the transcript).
        self.messages.append(AIMessage(content=text))
        self._cached_opener = text
        self._opened = True
        return text

    async def respond(self, agent_message: str) -> Dict[str, Any]:
        """Single agent->user round-trip.

        Returns a dict with keys: ``reply`` (str), ``stop`` (bool),
        ``budget_exceeded`` (bool), ``error`` (Optional[str]).
        """
        self.turn_count += 1
        if self.turn_count > self.max_user_turns:
            logger.info(
                f"User simulator budget exceeded after {self.max_user_turns} turns"
            )
            return {
                "reply": "[user-budget-exceeded]",
                "stop": True,
                "out_of_scope": False,
                "budget_exceeded": True,
                "error": None,
            }

        try:
            response = await self.llm_client.llm.ainvoke(
                self.messages + [HumanMessage(content=agent_message)]
            )
        except Exception as e:
            logger.exception("User simulator LLM call failed")
            return {
                "reply": "[user-simulator-error]",
                "stop": True,
                "out_of_scope": False,
                "budget_exceeded": False,
                "error": str(e),
            }

        self.messages.append(HumanMessage(content=agent_message))
        text = getattr(response, "content", "") or ""
        self.messages.append(AIMessage(content=text))

        stop = STOP_SENTINEL in text
        out_of_scope = OUT_OF_SCOPE_SENTINEL in text
        if stop:
            self.stop_emitted = True
            text = text.replace(STOP_SENTINEL, "").strip()
        if out_of_scope:
            self.out_of_scope_emitted = True
            text = text.replace(OUT_OF_SCOPE_SENTINEL, "").strip()

        # Out-of-scope is a distinct termination reason (the scenario lacks the
        # info needed to continue) but, like STOP, it ends the conversation — so
        # we set `stop` to drive the existing orchestrator exit path while
        # surfacing `out_of_scope` separately for logging/metrics.
        return {
            "reply": text,
            "stop": stop or out_of_scope,
            "out_of_scope": out_of_scope,
            "budget_exceeded": False,
            "error": None,
        }
