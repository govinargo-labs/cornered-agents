"""
agents/docs.py - the Docs Writer role.

Writes the product's README from the REAL code, after every task is built.
Why last? Docs written before the code describe what was planned, not what
was built, and the two drift apart. Docs written from the code can be checked.

It may read everything, but it can only write markdown files (write_doc),
so it cannot "fix" the code to match its docs.
"""

import tools
from agents.base import Agent

DOCS_TOOLS = [schema for schema in tools.TOOL_SCHEMAS if schema["name"] in ("list_files", "read_file")]
DOCS_TOOLS = DOCS_TOOLS + [tools.WRITE_DOC_SCHEMA]

DOCS_PROMPT = """\
You are the Docs Writer at a small open-source software company.
The product is built and tested. You write its README.md for new users.

How you work:
1. Call list_files. Read the main program, its command-line handling
   (argparse / sys.argv), and the tests: tests show real inputs and outputs.
2. Write README.md with write_doc. Sections:
   - name and one sentence: what it does and who it is for;
   - Requirements (Python version, any packages);
   - Usage: the exact commands, each with its real output;
   - Errors: what happens on bad input;
   - Running the tests: `python -m pytest`.
3. Reply with a one-line summary.

Rules:
- Document ONLY what the code really does. Every command and output in the
  README must match the code or the tests. If you are unsure, read the code
  again; never guess.
- Short and clear beats long. Use code blocks for commands and output.
- You can only write markdown (*.md) files. You never change code or tests.
"""


def make_docs_writer(**kwargs) -> Agent:
    """Create a Docs Writer agent. kwargs (e.g. client=) go to Agent."""
    kwargs.setdefault("max_steps", 10)
    return Agent("Docs", DOCS_PROMPT, tool_schemas=DOCS_TOOLS, **kwargs)
