# JAI Development Rules

1. Hardware & Budget Invariants:
   - Windows 11, Intel Core Ultra 5, 16 GB RAM (host RAM cap <= 8 GB).
   - $0.00 lifetime budget. Use free tiers: Gemini 2.5 Flash (`gemini-2.5-flash`), Groq Llama 3.3 70B (`llama-3.3-70b-versatile`), local Ollama (`qwen2.5:3b` loaded on-demand with keep_alive: "0").

2. Simplicity & YAGNI:
   - Prefer native Python standard library and direct Playwright CDP calls over complex ReAct abstractions or multi-agent loops.
   - For direct web actions (e.g., search on YouTube, open URL, fill known fields), execute direct Playwright macros rather than multi-turn LLM reasoning.

3. Rule 0 Safety Gate:
   - Non-destructive perception and navigation pass automatically.
   - Irreversible actions (mutating form submits, deleting records, purchases, typing credentials/passwords/OTPs) strictly halt for human approval via `src/action_gateway.py`.

4. Verification Oracle:
   - Always run `python -m py_compile <file>` after editing any Python file.
