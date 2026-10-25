"""
tests/fakes.py - a fake LLM for tests, shared by all agent tests.

It returns replies shaped like the real SDK's, in the order we script them.
"""

from types import SimpleNamespace


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_call(tool_id, name, tool_input):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def reply(stop_reason, *blocks):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=100, output_tokens=10),
    )


class FakeClient:
    """Pretends to be anthropic.Anthropic(). Returns scripted replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []  # every request our loop sent, so tests can inspect it
        self.messages = self  # so that client.messages.create(...) works

    def create(self, **request):
        # Copy the message list: the loop keeps appending to the same list later.
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.replies.pop(0)
