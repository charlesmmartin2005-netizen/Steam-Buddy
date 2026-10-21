"""The chat agent: Claude with the Steam tools in a tool-use loop.

The conversation history is append-only. Claude's thinking blocks are only
valid while everything before them stays byte-identical, so earlier turns are
never edited; a turn that fails is removed as a whole from the end.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import anthropic

from .config import Config
from .tools import TOOL_DEFINITIONS

MAX_TOKENS = 64_000  # thinking counts toward this as well as the reply
MAX_TOOL_ROUNDS = 15  # per question; then Claude is asked to answer with what it has

# On a policy decline the API re-runs the request on Anthropic's recommended
# fallback model instead of returning a refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5")

# Tool inputs stream as they're generated. tools.run validates every argument,
# so a truncated or malformed input comes back to Claude as an error.
_TOOLS = [{**t, "eager_input_streaming": True} for t in TOOL_DEFINITIONS]

SYSTEM_PROMPT = """\
You are Steam Buddy, a personal gaming assistant for one Steam user, chatting in their terminal. \
You can see their Steam library, playtime, achievements, wishlist and the Steam store through tools.

Today is {today}.

Ground everything in their data
- Call get_taste_profile before recommending anything, unless you already have it in this conversation.
- Every recommendation needs a reason tied to their history: name the games and the hours or tags \
that connect it, for example "you put 340 hours into Terraria and Dead Cells, and this shares their \
Roguelite and Pixel Graphics tags". A recommendation you can't connect to their data isn't worth making.
- Use what they bounced off (played under an hour) as a signal of what to avoid.
- Never suggest buying a game they already own. recommend_games, find_similar_games and \
find_hidden_gems already leave owned games out; in other results, "owned": true means it's in their library. Backlog and "worth going \
back to" answers are the opposite: those must be games they own.
- For open questions like "what should I buy next?", use recommend_games; for "games like X", \
find_similar_games; for lesser-known picks, find_hidden_gems (tags are optional). recommend_games and \
find_hidden_gems score each game against the user's taste, find_similar_games against the game or tags \
asked about. Build your reasons from because_you_played (the user's most similar games, with hours and \
shared tags) or shares_tags, and mention tags_you_tend_to_drop and early_access when present. match is \
only meaningful compared within one list.
- Before you recommend a game from those tools, check it with get_game_details if its reviews, price \
or release status matter to the answer.

Be honest about what the data can't tell you
- If a tool returns an error, an empty list or a note about missing data (a private profile, games \
without achievements, SteamSpy lag), say what's missing and how it limits your answer.
- Never invent hours, prices, review scores, release dates or news. If you didn't get it from a tool, \
don't state it as fact.
- Steam doesn't record session length or how long a game takes to finish. When you judge whether a game \
fits a short session, say it's based on its genre tags.
- The profile's "finished", "abandoned" and "bounced" labels are guesses from playtime and achievements; \
present them as likely, not certain.

How to reply
- The terminal shows plain text, so don't use markdown: no headings, tables, bold or links in brackets. \
Use short paragraphs and "-" bullet lists.
- Lead with the answer. A list of picks is usually three to five games, each with its name, a one-line \
reason, and the key facts (review score, price, discount).
- If the request is ambiguous, make a sensible assumption and say what you assumed, rather than asking.
"""


def build_system_prompt(today: date | None = None) -> str:
    return SYSTEM_PROMPT.format(today=(today or date.today()).strftime("%A, %B %d, %Y"))


class AgentError(Exception):
    """A turn couldn't complete; the message is safe to show the user."""


@dataclass
class Reply:
    text: str
    status: str = "ok"  # ok, refused, cut_off, gave_up
    fallback_model: str | None = None  # set when another model answered after a decline


ToolRunner = Callable[[str, dict[str, Any]], tuple[str, bool]]


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        run_tool: ToolRunner,
        *,
        model: str,
        effort: str,
        today: date | None = None,
        on_text: Callable[[str], None] | None = None,
        on_fallback: Callable[[str], None] | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ):
        self.client = client
        self.run_tool = run_tool
        self.model = model
        self.effort = effort
        self.on_text = on_text or (lambda _t: None)
        self.on_fallback = on_fallback or (lambda _m: None)
        self.max_tool_rounds = max_tool_rounds
        # Fixed for the session: changing it would invalidate earlier thinking and the cache.
        self.system = [{"type": "text", "text": build_system_prompt(today)}]
        self.messages: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, cfg: Config, run_tool: ToolRunner, **kwargs: Any) -> Agent:
        """Build an agent; `run_tool` is usually SteamTools.run, maybe wrapped for display."""
        if not cfg.anthropic_api_key:
            raise AgentError("ANTHROPIC_API_KEY is not set.")
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key.reveal())
        return cls(client, run_tool, model=cfg.anthropic_model, effort=cfg.anthropic_effort, **kwargs)

    def reset(self) -> None:
        self.messages = []

    # -- one question -----------------------------------------------------------

    def ask(self, question: str) -> Reply:
        """Answer one user message, running tools as Claude asks for them."""
        start = len(self.messages)
        self.messages.append({"role": "user", "content": question})
        try:
            return self._run_turn(start)
        except BaseException:
            # Ctrl+C, a network error, or a bug: drop the whole unfinished turn so
            # the history never ends with a tool call that has no result.
            del self.messages[start:]
            raise

    def _run_turn(self, start: int) -> Reply:
        rounds = 0
        fallback_model = None
        while True:
            response = self._call()
            if _fallback_ran(response):
                fallback_model = response.model

            if response.stop_reason == "refusal":
                del self.messages[start:]
                return Reply(
                    "Claude declined to answer that one, so it was left out of the conversation. "
                    "Try rephrasing it.",
                    status="refused",
                )
            if response.stop_reason == "max_tokens":
                del self.messages[start:]
                return Reply("The answer ran past the length limit and was cut off. Try a narrower question.",
                             status="cut_off")

            content = _echo_content(response.content)
            self.messages.append({"role": "assistant", "content": content})
            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                text = "".join(b.text for b in content if b.type == "text").strip()
                return Reply(text, fallback_model=fallback_model)

            rounds += 1
            if rounds > self.max_tool_rounds + 2:
                # It was told twice to stop calling tools; give up on this question.
                del self.messages[start:]
                return Reply("That took more lookups than allowed for one question. Try narrowing it down.",
                             status="gave_up")
            self.messages.append({"role": "user", "content": self._results(tool_uses, rounds)})

    def _results(self, tool_uses: list[Any], rounds: int) -> list[dict[str, Any]]:
        """All tool results for one assistant turn, in one user message."""
        results = []
        for block in tool_uses:
            if rounds > self.max_tool_rounds:
                text, is_error = json.dumps({
                    "error": "Lookup limit for this question reached. Answer now with what you have, "
                             "and say what you couldn't check."
                }), True
            else:
                text, is_error = self.run_tool(block.name, block.input)
            result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
            if is_error:
                result["is_error"] = True
            results.append(result)
        return results

    def _call(self, retries: int = 2) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "tools": _TOOLS,
            "messages": self.messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.model.startswith(_FALLBACK_MODELS):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        for attempt in range(retries + 1):
            try:
                with self.client.beta.messages.stream(**kwargs) as stream:
                    for event in stream:
                        if event.type == "text":
                            self.on_text(event.text)
                        elif event.type == "content_block_start" and event.content_block.type == "fallback":
                            self.on_fallback(event.content_block.to.model)
                    return stream.get_final_message()
            except ValueError:
                # The SDK couldn't parse a streamed tool input at all; ask again.
                if attempt == retries:
                    raise AgentError("Claude's reply came back garbled twice. Please try again.") from None
            except anthropic.AuthenticationError:
                raise AgentError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from None
            except anthropic.PermissionDeniedError:
                raise AgentError(f"Your Anthropic account can't use {self.model}. "
                                 "Set ANTHROPIC_MODEL in .env to a model you have access to.") from None
            except anthropic.NotFoundError:
                raise AgentError(f"Model {self.model!r} wasn't found. Check ANTHROPIC_MODEL in .env.") from None
            except anthropic.RateLimitError:
                raise AgentError("Hit the Anthropic rate limit. Wait a minute and try again.") from None
            except anthropic.APIConnectionError:
                raise AgentError("Couldn't reach the Anthropic API. Check your connection.") from None
            except anthropic.APIStatusError as e:
                raise AgentError(f"The Anthropic API returned an error ({e.status_code}). Try again.") from None
        raise AssertionError("unreachable")


def _fallback_ran(response: Any) -> bool:
    """True when a fallback model served this response (including sticky turns with no fallback block)."""
    iterations = getattr(response.usage, "iterations", None) or []
    return response.stop_reason != "refusal" and any(
        getattr(i, "type", None) == "fallback_message" for i in iterations
    )


_KEEP_BEFORE_FALLBACK = {"text", "fallback"}


def _echo_content(content: list[Any]) -> list[Any]:
    """The assistant content to send back next turn.

    Normally that's the content unchanged. After a mid-answer fallback, the
    declined model's thinking and tool calls before the last fallback marker
    are dropped (its text is kept), as the API requires.
    """
    last = max((i for i, b in enumerate(content) if b.type == "fallback"), default=None)
    if last is None:
        return list(content)
    return [b for i, b in enumerate(content) if i >= last or b.type in _KEEP_BEFORE_FALLBACK]
