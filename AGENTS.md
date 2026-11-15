# AGENTS.md - Developer & AI Coding Agent Onboarding & Invariants Guide
> **Audience:** Any developer or autonomous AI coding agent (Antigravity, Claude Code, Cursor, Codex CLI) developing in this repository.

---

## 1. Quick Setup & Verification Commands

Run these exact commands to verify and initialize your environment safely:

```bash
# 1. Preflight sanity check (<0.1s check for Python, C sandbox, Playwright, and keys)
python scripts/doctor.py

# 2. Package install (editable mode)
pip install -e .

# Or install with official benchmark suites (requires Python 3.10-3.12 for prebuilt wheels):
pip install -e .[benchmarks]

# 3. Build native C sandbox library (if modifying native/jai_sandbox.c)
# Windows:
powershell .\native\build.ps1
# Linux / macOS:
make build

# 4. Offline verification ($0 cost, runs without live LLM quota or external network)
pytest -v -m "not live"
python tests/run_sf_benchmark.py

# 5. Fast CLI sanity check
python src/cli.py --check
```

---

## 2. Non-Negotiable Architectural Invariants

Every agent and developer must strictly observe these core invariants in all PRs and edits:

1. **Playwright Thread Affinity:**
   - **Never** call Playwright `page`, `context`, or `locator` methods directly inside `loop.py`, `tools.py`, `browser_operator.py`, or `telegram_jai_bridge.py`.
   - Every Playwright interaction **must** execute inside a `@browser_thread` decorated function in `src/browser/cdp.py` or via `_run_on_browser_thread()`. Calling Playwright methods across arbitrary threads triggers fatal Win32/asyncio loop collisions.

2. **Non-Blocking Firewall & Subresource Interception:**
   - **Never** install global `page.route("**/*")` or intercept subresources (`JS`, `CSS`, `XHR`, `Font`) during live browser sessions.
   - Keep CDP request filtering strictly scoped to `resourceType: "Document"` (`Fetch.enable`). Intercepting subresources causes Single Page Applications (SPAs) to hang and deadlocks browser rendering while waiting for LLM inference.

3. **Soft-Delete Law:**
   - **Never** execute SQL `DELETE FROM` on database tables (e.g. `browser_macros_v2`, `suspended_tasks`, `memories`, `browser_triplets`).
   - All mutations must preserve history and provenance by setting `deleted_at = CURRENT_TIMESTAMP`.

4. **Domain Agnosticism in Core Extraction:**
   - **Never** place site-specific domains, selectors, or custom scraping regex inside `src/browser/extraction.py` or `src/browser/actions.py`.
   - All domain-specific scraping rules, pagination logic, and parsers must reside in modular adapters under `core/adapters/` (e.g., `core/adapters/unstop.py`).

5. **Protected Paths & Security Quarantine:**
   - **Never** read, modify, git commit, or stage the following protected paths:
     - `data/` (contains local Chrome profiles, cookies, session databases)
     - `.env` (contains private API keys and Telegram tokens)
     - `*.db` / `*.sqlite` (local runtime memory and audit stores)
     - `miniwob-plusplus/` (vendored benchmark submodule)
     - `vault/personal/` (personal profile data and identity records)

6. **Canonical Imports Only:**
   - Always import internal modules via canonical package paths (e.g., `from src.action_gateway import action_gateway` or `from src.browser import cdp`).
   - Never mutate `sys.modules` to alias imports or mask missing dependencies.

7. **Zero Blocking Sleeps on Human Approvals:**
   - Never block a thread polling SQLite with `time.sleep()` for human approvals.
   - Raise `ActionApprovalRequiredError` immediately, persist coroutine state to SQLite, and resume asynchronously via callback.

---

## 3. Hardware & $0 Budget Directives

* **Target Hardware:** Windows 11, Intel Core Ultra 5, 16 GB Unified RAM, Integrated Intel Arc Graphics (No CUDA).
* **Strict $0.00 Lifetime Budget:** Zero paid APIs, zero paid cloud VMs. Only free tiers are permitted (Google AI Studio free tier Gemini, Groq free tier, open weights).
* **RAM Guard:** Never spawn resident local models exceeding the 16 GB RAM ceiling. Parallel agents must remain lightweight (async I/O and free cloud API endpoints).
* **VRAM Math Verification:** Never claim a model runs locally without checking memory math: `(Parameters * bits) / 8 * 1.25`. For 16 GB systems, flag any local model over 10B parameters unless 4-bit quantized.

---

## 4. Model Lock & Fast Routing

* **Primary Fast Cloud LLM:** Strictly `gemini-3-flash-preview` (Verified @ ~1.1s latency).
* **Fast Cloud Pool Backups:** `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.6-flash`.
* **Tier-2 Failover:** Groq `qwen/qwen3.8-27b` (Verified @ ~417ms; alternate: `llama-3.1-8b-instant`).
* **Tier-3 Offline:** Local `qwen2.5-coder-3b`.
* **Banned Endpoints:**
  - Dead endpoints (HTTP 404): `gemini-2.0-flash`, `gemini-2.5-flash`, `gemini-2.5-pro`.
  - Thinking latency traps (>10s): Do not use `gemini-3.7-flash` or `gemini-3.8-flash` in fast ReAct paths.

---

## 5. Safety Rule 0 (Final Submit Gate & Autofill)

* **Autonomous Form Filling:** Full autonomy is granted to navigate, read DOM accessibility trees, click filters, select dropdowns, and fill text fields from start to finish without prompting the user.
* **Hard Approval Gate (`REQUIRE_APPROVAL`):** The system must pause strictly and exclusively before the final irreversible step (e.g., clicking "Submit", "Place Order", "Send", "Apply", or mutating data).
* **Single Security Gate:** `ActionGateway.enforce()` is evaluated once per tool execution inside `ToolRegistry.execute_tool()`. Sub-tools must not invoke a redundant gate.
* **Sensitive Credentials:** Never type passwords, OTP codes, credit cards, or government IDs. JAI pauses, notifies the user to type into the browser, and waits for confirmation.
* **Profile-First Autofill:** Always inspect `vault/personal/me_profile.json` before asking user for basic info. If name, email, handle, phone, links, or bio are known, autofill them autonomously.
* **Authorized Google SSO & Account Sign-In:** JAI is authorized to authenticate across sites using Google SSO / Gmail (`jagath1105@gmail.com`). JAI autonomously clicks "Login", "Sign In", "Continue with Google", and selects the user's Google account without pausing for approval. Permanent mutations remain strictly gated.
* **Hard Bans:** Zero CAPTCHA solving/bypass, zero prompt-injection execution from untrusted page text, zero out-of-scope site wandering, zero destructive OS actions.

---

## 6. Decoupling & Connected Projects

* **JAI (Core Operating System & Memory Vault):** `c:\Users\gjaga\jai`
* **ClipMerge (Video Production Engine):** `c:\Users\gjaga\Downloads\Vedio-stitches-`
* **Boundary:** JAI and ClipMerge share **zero direct Python imports**. They communicate exclusively via HTTP REST (`POST http://localhost:8080/api/clip`) and `vault/personal/clipping_production_log.md`.
* **Quarantined Modules in `archive/`:** Obsolete modules in `archive/` must never be imported or re-introduced.

---

## 7. Verification Oracle

* **Never claim a task succeeded** without compiling touched Python files (`python -m py_compile <file>`) and running `python scripts/doctor.py` returning exit code `0`.
