from datetime import date
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from steam_buddy.agent import FALLBACK_BETA, Agent, AgentError, build_system_prompt


def text(t):
    return NS(type="text", text=t)


def tool_use(id, name, input=None):
    return NS(type="tool_use", id=id, name=name, input=input or {})


def thinking():
    return NS(type="thinking", thinking="", signature="sig")


def fallback(to="claude-opus-5"):
    return NS(type="fallback", to=NS(model=to))


def message(*content, stop="end_turn", model="claude-opus-5-5", iterations=None):
    return NS(content=list(content), stop_reason=stop, model=model, usage=NS(iterations=iterations))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.msg.content:
            yield NS(type="content_block_start", content_block=block)
            if block.type == "text":
                yield NS(type="text", text=block.text)

    def get_final_message(self):
        return self.msg


class FakeAnthropic:
    """Replays scripted responses and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kwargs):
        # Copy the list: the agent keeps appending to the same one.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return FakeStream(r)


class Tools:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, name, args):
        self.calls.append((name, args))
        r = self.results.get(name, ('{"ok": true}', False))
        if isinstance(r, BaseException):
            raise r
        return r


def make_agent(*responses, model="claude-opus-5-5", tools=None, **kw):
    client = FakeAnthropic(*responses)
    tools = tools or Tools()
    streamed = []
    agent = Agent(client, tools, model=model, effort="medium", today=date(2026, 10, 21),
                  on_text=streamed.append, **kw)
    return agent, client, tools, streamed


def test_plain_answer_streams_text():
    agent, client, tools, streamed = make_agent(message(text("Play Hades.")))
    reply = agent.ask("what should I play?")
    assert reply.text == "Play Hades."
    assert reply.status == "ok"
    assert streamed == ["Play Hades."]
    assert tools.calls == []
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_tool_loop_returns_results_in_one_message():
    tools = Tools({"get_backlog": ('{"error": "nope"}', True)})
    agent, client, _, _ = make_agent(
        message(thinking(), tool_use("t1", "get_wishlist"), tool_use("t2", "get_backlog", {"max_hours": 2}),
                stop="tool_use"),
        message(text("Here you go.")),
        tools=tools,
    )
    reply = agent.ask("anything on sale?")
    assert reply.text == "Here you go."
    assert tools.calls == [("get_wishlist", {}), ("get_backlog", {"max_hours": 2})]
    results = agent.messages[2]
    assert results["role"] == "user"
    assert [r["tool_use_id"] for r in results["content"]] == ["t1", "t2"]
    assert "is_error" not in results["content"][0]
    assert results["content"][1]["is_error"] is True
    # The thinking block goes back unchanged so the model keeps its reasoning.
    assert agent.messages[1]["content"][0].type == "thinking"


def test_request_shape():
    agent, client, _, _ = make_agent(message(text("a")), message(text("b")))
    agent.ask("one")
    agent.ask("two")
    first, second = client.requests
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["betas"] == [FALLBACK_BETA]
    assert first["output_config"] == {"effort": "medium"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in first  # always on for this model; omitting it is the supported form
    assert all(t["eager_input_streaming"] for t in first["tools"])
    # System prompt and tools stay identical so earlier thinking and the cache stay valid.
    assert first["system"] == second["system"]
    assert first["tools"] == second["tools"]
    assert second["messages"][:2] == first["messages"] + [agent.messages[1]]


def test_no_fallbacks_for_models_without_them():
    agent, client, _, _ = make_agent(message(text("hi")), model="claude-haiku-5-5")
    agent.ask("hi")
    assert "fallbacks" not in client.requests[0]
    assert "betas" not in client.requests[0]


def test_refusal_drops_the_turn():
    agent, client, _, _ = make_agent(message(text("ok")), message(stop="refusal"))
    agent.ask("first")
    before = list(agent.messages)
    reply = agent.ask("second")
    assert reply.status == "refused"
    assert agent.messages == before


def test_truncated_tool_call_is_not_run():
    agent, client, tools, _ = make_agent(message(tool_use("t1", "get_backlog", {"max_h"}), stop="max_tokens"))
    reply = agent.ask("backlog?")
    assert reply.status == "cut_off"
    assert tools.calls == []
    assert agent.messages == []


def test_interrupted_turn_is_rolled_back():
    tools = Tools({"get_wishlist": KeyboardInterrupt()})
    agent, client, _, _ = make_agent(message(tool_use("t1", "get_wishlist"), stop="tool_use"), tools=tools)
    with pytest.raises(KeyboardInterrupt):
        agent.ask("wishlist?")
    assert agent.messages == []


def test_api_errors_become_friendly_and_roll_back():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    agent, client, _, _ = make_agent(err)
    with pytest.raises(AgentError, match="ANTHROPIC_API_KEY"):
        agent.ask("hi")
    assert agent.messages == []


def test_garbled_tool_input_is_retried():
    agent, client, _, _ = make_agent(ValueError("bad json"), message(text("fine")))
    assert agent.ask("hi").text == "fine"
    assert len(client.requests) == 2


def test_tool_rounds_are_capped():
    loop = [message(tool_use(f"t{i}", "get_wishlist"), stop="tool_use") for i in range(4)]
    agent, client, tools, _ = make_agent(*loop, max_tool_rounds=1)
    reply = agent.ask("loop forever")
    assert reply.status == "gave_up"
    assert len(tools.calls) == 1  # later rounds got "limit reached" errors instead of running
    assert agent.messages == []


def test_pause_turn_continues():
    agent, client, _, _ = make_agent(message(text("Working"), stop="pause_turn"), message(text(" done")))
    reply = agent.ask("go")
    assert reply.text == "done"
    assert len(client.requests) == 2


def test_mid_answer_fallback_echo_and_tools():
    msg = message(
        thinking(), text("Let me "), tool_use("old", "get_wishlist"),
        fallback(), text("Checking."), tool_use("new", "get_backlog"),
        stop="tool_use", iterations=[NS(type="message"), NS(type="fallback_message")],
        model="claude-opus-5",
    )
    agent, client, tools, _ = make_agent(msg, message(text("Done."), model="claude-opus-5",
                                                       iterations=[NS(type="fallback_message")]))
    reply = agent.ask("help")
    # Only the tool call after the switch runs; the declined model's thinking and call are dropped.
    assert tools.calls == [("get_backlog", {})]
    assert [b.type for b in agent.messages[1]["content"]] == ["text", "fallback", "text", "tool_use"]
    assert reply.fallback_model == "claude-opus-5"


def test_reset_clears_history():
    agent, *_ = make_agent(message(text("a")))
    agent.ask("x")
    agent.reset()
    assert agent.messages == []


def test_system_prompt_has_date_and_rules():
    prompt = build_system_prompt(date(2026, 10, 21))
    assert "Wednesday, October 21, 2026" in prompt
    assert "Never suggest buying a game they already own" in prompt
