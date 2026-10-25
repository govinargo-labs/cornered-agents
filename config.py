"""
config.py - which LLM our agents talk to.

Two options, chosen in .env (no code changes needed to switch):

  Local model (free, private, runs on your Mac via oMLX):
      LLM_BASE_URL=http://localhost:8001
      LLM_MODEL=Qwen3.6-35B-A3B-MLX-4bit
      (the key is read from the API_KEY_LLM environment variable)

  Claude in the cloud (smarter, costs money):
      leave LLM_BASE_URL empty, set ANTHROPIC_API_KEY

Why this works: oMLX speaks the same "Messages API" as Anthropic, so the same
anthropic SDK and the same agent loop work for both. Only the address changes.
"""

import os

import anthropic
from dotenv import load_dotenv

load_dotenv()  # copy the values from .env into os.environ

# Note: the SDK adds "/v1/messages" itself, so the base URL must NOT end in /v1.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().removesuffix("/").removesuffix("/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")

# Thinking = the model reasons in hidden "thinking" text before it acts.
# Better decisions, but every thinking token must be generated, which is slow
# on a local model. LLM_THINKING=off skips it (local oMLX models only: Claude
# models control this with effort instead, and reject "disabled").
LLM_THINKING = os.getenv("LLM_THINKING", "on").strip().lower()
if LLM_THINKING not in ("on", "off"):
    raise SystemExit(f"LLM_THINKING must be 'on' or 'off', not '{LLM_THINKING}'.")
if LLM_THINKING == "off" and not LLM_BASE_URL:
    raise SystemExit("LLM_THINKING=off only works with a local model (LLM_BASE_URL). "
                     "For Claude, keep it 'on' and lower the agent's effort instead.")


# Context budget: stop an agent once its conversation is bigger than this many
# tokens. oMLX here allows 32k per request, and big requests can also run the Mac
# out of memory, so stay well below that. Claude models allow far more.
CONTEXT_BUDGET = int(os.getenv("LLM_CONTEXT_BUDGET", "24000" if LLM_BASE_URL else "150000"))


def request_options() -> dict:
    """Extra settings sent with every model request."""
    if LLM_THINKING == "off":
        return {"thinking": {"type": "disabled"}}
    return {}


def make_client() -> anthropic.Anthropic:
    if LLM_BASE_URL:
        api_key = os.getenv("API_KEY_LLM")
        if not api_key:
            raise SystemExit("LLM_BASE_URL is set, but the API_KEY_LLM environment variable is missing.")
        # Local models can be slow; give each reply up to 10 minutes.
        # max_retries=1: if the local server fails (e.g. out of memory), retrying the
        # same big request over and over rarely helps.
        return anthropic.Anthropic(base_url=LLM_BASE_URL, api_key=api_key, timeout=600, max_retries=1)
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
