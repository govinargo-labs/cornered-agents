"""
agents/base.py - the agent loop. Every agent in our company uses this.

An "agent" is just a while-loop around an LLM call:

    messages = [task]
    repeat (at most max_steps times):
        response = ask the model (system prompt + tools + all messages so far)
        if the model wants tools -> run them, add the results to messages, loop again
        else                     -> the model is finished, stop

That's it. The Developer, QA, PM and CEO agents will all be this same loop.
What makes them different is only:
  - the system prompt (their role and rules), and
  - which tools they get.
"""

from dataclasses import dataclass, field

import anthropic

import config
import run_log
import tools as tools_module

MAX_TOKENS = 16_000  # max length of ONE model reply (not the whole run)
MAX_NUDGES = 2       # reminders to call the finish tool before we give up


@dataclass
class AgentResult:
    """What run() returns, so the caller (later: the company orchestrator) can decide what to do next."""
    final_text: str        # the model's last message, e.g. "Done: wrote slugify.py, 6 tests pass"
    stop_reason: str       # "done", "max_steps", "max_tokens", "refusal", "context_limit",
                           # "api_error" or "no_finish_tool"
    steps: int             # how many model calls we made
    input_tokens: int = 0  # token totals = what this run cost
    output_tokens: int = 0
    output: dict | None = None  # the finish tool's arguments, e.g. QA's {"verdict": "pass", ...}
    messages: list = field(default_factory=list)  # the full conversation, for debugging


class Agent:
    def __init__(self, name, system_prompt, tool_schemas=None, max_steps=20, effort="medium",
                 client=None, finish_tool=None, force_finish=False):
        self.name = name
        self.system_prompt = system_prompt
        self.tool_schemas = tools_module.TOOL_SCHEMAS if tool_schemas is None else tool_schemas
        # Each role may ONLY use the tools it was given (e.g. QA cannot write files).
        self.allowed_tools = {schema["name"] for schema in self.tool_schemas}
        # Optional "I'm done, here is my answer" tool. When the model calls it, we
        # save its arguments in result.output and stop. This gives us structured
        # data (like {"verdict": "fail", ...}) instead of free text we must parse.
        self.finish_tool = finish_tool
        # For one-shot jobs (DevOps: "write a commit message"), the model must call
        # the finish tool right away; the API's tool_choice makes that mandatory.
        # (Claude rejects a forced tool_choice when extended thinking is on.)
        self.force_finish = force_finish and finish_tool is not None
        self.max_steps = max_steps
        self.effort = effort  # how hard the model thinks: "low" / "medium" / "high"
        # config.make_client() connects to the local model or to Claude (see config.py).
        # Tests pass in a fake client instead, so they need no key and cost nothing.
        self.client = client or config.make_client()

    def log(self, text):
        print(f"[{self.name}] {text}")

    def _ask_model(self, messages, tool_schemas=None, force=False):
        """One request to the LLM with everything it needs to decide the next step.
        tool_schemas: offer only these tools this time (default: all of the agent's tools).
        force: the model MUST call the finish tool in this reply."""
        return self.client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=MAX_TOKENS,
            # cache_control marks "everything up to here (tools + system prompt) is
            # the same every call: cache it". Both oMLX and Claude then report
            # cache hits in response.usage, and Claude also charges less for them.
            system=[{"type": "text", "text": self.system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            tools=self.tool_schemas if tool_schemas is None else tool_schemas,
            output_config={"effort": self.effort},
            messages=messages,
            **({"tool_choice": {"type": "tool", "name": self.finish_tool}}
               if self.force_finish or force else {}),
            **config.request_options(),  # e.g. thinking off for a local model (see config.py)
        )

    def run(self, task: str) -> AgentResult:
        # The conversation. The model has NO memory between calls:
        # we send the whole list every time, and it grows as we go.
        messages = [{"role": "user", "content": task}]
        result = AgentResult(final_text="", stop_reason="max_steps", steps=0, messages=messages)
        run_log.write(self.name, "agent_start", model=config.LLM_MODEL, thinking=config.LLM_THINKING,
                      system=self.system_prompt,
                      tools=sorted(self.allowed_tools), task=task)

        nudges = 0
        for step in range(1, self.max_steps + 1):
            result.steps = step
            self.log(f"--- step {step}/{self.max_steps}: asking the model ---")

            # Last step: an agent with a finish tool must hand in its answer NOW,
            # or all its work is lost (QA once spent 15 steps investigating and
            # never gave a verdict). A warning alone was ignored ("Let me verify
            # once more..."), so we ALSO offer only the finish tool this time.
            # Even that was ignored: a local model called run_tests on its last
            # step (a tool it had seen earlier in the conversation), so QA never
            # gave a verdict. So with a local model we FORCE the call through the
            # API (tool_choice). Claude rejects a forced call while thinking is on,
            # but it does respect the list of offered tools.
            offered = None
            force = False
            if step == self.max_steps and self.finish_tool and step > 1:
                offered = [t for t in self.tool_schemas if t["name"] == self.finish_tool]
                force = config.LLM_THINKING == "off"
                self.log(f"last step -> telling the model to call {self.finish_tool}")
                run_log.write(self.name, "last_step_warning", step=step)
                _add_text(messages[-1], (
                    f"This is your LAST step. Do not call any other tool. Call {self.finish_tool} "
                    "now with your best answer, based on what you found so far."
                ))

            try:
                response = self._ask_model(messages, offered, force)
            except anthropic.APIError as e:
                # Server error, timeout, lost connection... Stop THIS agent cleanly and
                # report it, instead of crashing the whole company run.
                result.stop_reason = "api_error"
                result.final_text = f"API error: {e}"
                self.log(f"stopped: API error: {e}")
                run_log.write(self.name, "api_error", step=step, error=str(e))
                break

            # With caching, the prompt is split over three counters. Add them up to
            # get how many tokens the model really had to read this step.
            usage = response.usage
            cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
            tokens_in = usage.input_tokens + cache_read + cache_write
            result.input_tokens += tokens_in
            result.output_tokens += usage.output_tokens
            # Full reply, including thinking, text and tool calls: nothing is cut here.
            run_log.write(self.name, "model_reply", step=step, stop_reason=response.stop_reason,
                          content=response.content, input_tokens=tokens_in,
                          cache_read_tokens=cache_read, output_tokens=usage.output_tokens)

            # Save the model's reply exactly as it came back (text, thinking AND
            # tool_use blocks). The next request must include it unchanged.
            messages.append({"role": "assistant", "content": response.content})

            text = "\n".join(b.text for b in response.content if b.type == "text")
            if text:
                self.log(f"model says: {text}")
                result.final_text = text

            # The model stopped without calling its finish tool, e.g. it wrote
            # "I will now submit this plan." and ended. Small models do this a lot.
            # Remind it (a "nudge") instead of failing the whole run.
            if response.stop_reason == "end_turn" and self.finish_tool:
                if nudges < MAX_NUDGES:
                    nudges += 1
                    self.log(f"no {self.finish_tool} call yet -> reminder {nudges}/{MAX_NUDGES}")
                    run_log.write(self.name, "nudge", step=step, count=nudges)
                    messages.append({"role": "user", "content": (
                        f"You have not called {self.finish_tool} yet. Your work only counts when you "
                        f"call the {self.finish_tool} tool. Call it now with your final answer."
                    )})
                    continue
                result.stop_reason = "no_finish_tool"
                self.log(f"stopped: never called {self.finish_tool}")
                break

            # stop_reason tells us WHY the model stopped writing.
            if response.stop_reason != "tool_use":
                result.stop_reason = {
                    "end_turn": "done",         # finished normally
                    "max_tokens": "max_tokens",  # reply got cut off (MAX_TOKENS too small)
                    "refusal": "refusal",        # model declined the request
                }.get(response.stop_reason, response.stop_reason)
                self.log(f"stopped: {result.stop_reason}")
                break

            # Did the model call the finish tool? Then save its answer and stop.
            finish = next((b for b in response.content
                           if b.type == "tool_use" and b.name == self.finish_tool), None)
            if finish is not None:
                result.output = finish.input
                result.stop_reason = "done"
                self.log(f"finished with {finish.name}: {_short(finish.input)}  (preview; full output in the log)")
                break

            # The model asked for one or more tools. Run each and collect results.
            # Only tools offered in THIS request may run: a tool the agent may use
            # in general is still refused on a step where it was not offered.
            allowed_now = self.allowed_tools if offered is None else {t["name"] for t in offered}
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                self.log(f"tool call: {block.name}({_short(block.input)})")
                if block.name in allowed_now:
                    output, is_error = tools_module.run_tool(block.name, block.input)
                elif block.name in self.allowed_tools:
                    output, is_error = f"Tool '{block.name}' is not available on this step.", True
                else:
                    output, is_error = f"Tool '{block.name}' is not available to you.", True
                self.log(f"tool {'ERROR' if is_error else 'result'}: {_short(output)}")
                run_log.write(self.name, "tool_result", step=step, name=block.name,
                              input=block.input, output=output, is_error=is_error)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # links this result to the request it answers
                    "content": output,
                    "is_error": is_error,
                })

            # All results go back together in ONE user message.
            messages.append({"role": "user", "content": tool_results})

            # Context budget: the conversation only grows. If it is already this big,
            # the next request may not fit in the model's context window (or memory).
            # Stop now; the work so far is saved in the workspace files.
            if tokens_in > config.CONTEXT_BUDGET:
                result.stop_reason = "context_limit"
                self.log(f"stopped: conversation is {tokens_in} tokens, budget is {config.CONTEXT_BUDGET}")
                break
        else:
            # The for-loop ran out without a break: the agent never finished.
            self.log(f"stopped: hit the step limit ({self.max_steps})")

        self.log(f"tokens used: {result.input_tokens} in, {result.output_tokens} out")
        run_log.write(self.name, "agent_end", stop_reason=result.stop_reason, steps=result.steps,
                      input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                      final_text=result.final_text, output=result.output)
        return result


def _add_text(message, text):
    """Add a text note to a user message. Its content is either a string (a task
    or a reminder) or a list of blocks (tool results); handle both."""
    if isinstance(message["content"], str):
        message["content"] += f"\n\n{text}"
    else:
        message["content"].append({"type": "text", "text": text})


def _short(value, limit=200):
    """Shorten long text for the console (the full text still goes to the model)."""
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."
