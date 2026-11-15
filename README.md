# JAI (Just Autonomous Intelligence) 🌐⚡

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![Budget](https://img.shields.io/badge/API_Cost-$0.00_Lifetime-brightgreen)](#-strict-000-lifetime-budget)
[![Benchmark](https://img.shields.io/badge/Adversarial_Defense-100%25_Guarded-success)](#-empirical-benchmark-results)
[![Process Containment](https://img.shields.io/badge/Sandbox-Native_C_Kernel_Job_Objects-orange)](#-native-c-process-containment)

Autonomous, high-speed, secure browser agent runtime engineered for zero-friction setup, local kernel process containment, and $0 lifetime API cost.

---

## ⚡ 1-Minute Quickstart

Clone the repository and run the zero-dependency preflight check:

```bash
# 1. Clone repository
git clone https://github.com/jagath0511-ui/jai.git
cd jai

# 2. Run instant preflight sanity check (<0.1s execution time)
python scripts/doctor.py

# 3. Install in editable mode
pip install -e .

# 4. Run an autonomous browsing task (3-tier automatic browser resolution)
jai "Search Wikipedia for Quantum Computing"
```

> [!TIP]
> **No API Key?** On first run, `jai` will interactively prompt you for a free [Google AI Studio Gemini API key](https://aistudio.google.com/app/apikey) and automatically save it to `.env`, or you can run offline mock mode: `jai --mock "Task"`.

---

## 🚀 Key Architectural Highlights

### 1. Automatic 3-Tier Zero-Setup Browser Resolver
New users never need to manually download browser binaries:
* **Tier 1 (Google Chrome):** Auto-attaches to system Google Chrome via native CDP.
* **Tier 2 (Microsoft Edge):** If Chrome is absent on Windows, automatically launches Microsoft Edge (`channel="msedge"`). Zero downloads required for Windows users.
* **Tier 3 (Self-Healing Bundled Chromium):** On Linux / headless CI environments, defaults to Playwright Chromium. If missing, automatically installs Chromium once and retries.

### 2. Native C Kernel Process Containment (`jai_sandbox`)
Microsecond process boundary enforcement:
* **Windows:** Win32 Job Objects (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `ProcessMemoryLimit = 2048MB`, `ActiveProcessLimit = 16`) + Low Integrity Level tokens (`S-1-16-4096`).
* **Linux / POSIX:** Kernel `setrlimit` memory caps + `prctl(PR_SET_NO_NEW_PRIVS)`.
* **Pre-compiled in release wheels:** Zero compiler requirement on end-user machines.

### 3. Strict $0.00 Lifetime Budget
* Engineered specifically for $0 free-tier cloud reasoning: Primary fast path on **Gemini 3 Flash Preview** (~1.1s latency) and **Groq Qwen 2.5 27B** failover (~417ms).
* Zero paid cloud VMs, zero paid API dependencies.

### 4. Immune to Indirect Prompt Injection
* **Untrusted DOM Quarantine:** Encapsulates web content in untrusted delimiters before LLM evaluation.
* **Deterministic Macro Replay:** Frequently repeated workflows compile into zero-LLM local macros, reducing API latency and cost to absolute zero.

---

## 📊 Empirical Benchmark Results

Evaluated on 15 adversarial, DOM drift, and multi-step macro scenarios ([BENCHMARK_REPORT.md](BENCHMARK_REPORT.md)):

| Category | Test Scenarios | Pass Rate | Mean Wall Time | Attack Block Rate | LLM Calls Saved |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Indirect Prompt Injection** | 5 | **100.0%** | 107.0 ms | 🛡️ **100.0%** | 0 |
| **DOM Drift & UI Interruptions** | 5 | **100.0%** | 105.4 ms | 🛡️ **100.0%** | 2 |
| **Macro Compilation & Budget** | 5 | **100.0%** | 154.2 ms | 🛡️ **100.0%** | 10 |
| **Overall Scorecard** | **15** | **15 / 15 (100%)** | **122.2 ms** | 🛡️ **100.0%** | **12 calls** |

To reproduce benchmark results locally ($0 cost, offline fixtures):
```bash
python tests/run_sf_benchmark.py
```

---

## 🛠️ CLI Reference

```bash
# Run system diagnostic audit (browsers, C sandbox, credentials)
jai --check

# Browse a target URL in visible window
jai --url "https://news.ycombinator.com" "Extract the top 5 articles"

# Run in headless mode
jai --headless "Look up the weather in Tokyo"

# Run reproducible MiniWoB++ benchmark suite
jai --benchmark miniwob --max-tasks 5
```

---

## 🤖 Developer & AI Agent Guidelines

If you are developing or contributing using an AI coding agent (Antigravity, Claude Code, Cursor, Codex CLI), strictly review [AGENTS.md](AGENTS.md) before making edits. All architectural invariants, thread affinity rules, and verification oracles are documented there.

---

## 📄 License
Licensed under the [Apache License, Version 2.0](LICENSE).
