# Antigravity Unified Workspace & Cross-Session Memory Protocol

You are operating within the user's unified multi-project architecture. All projects, memory vaults, and conversation histories are permanently linked.

## 1. Connected Projects & Roles
1. **JAI (Core Memory & Knowledge Vault)**: `c:\Users\gjaga\jai`
   - Master Build Plan: `c:\Users\gjaga\jai\jai build plan.md`
   - Casting Bible & TTS Voices: `c:\Users\gjaga\jai\vault\personal\concepts\classroom_of_the_elite_casting.md`
   - Master Conversation History Index: `c:\Users\gjaga\jai\vault\antigravity_hub\CONVERSATION_HISTORY_INDEX.md`
   - Memory Search Utility: `python c:\Users\gjaga\jai\src\cross_conversation_memory.py --search "<term>"`
2. **ClipMerge (Autonomous Video Production Engine)**: `c:\Users\gjaga\Downloads\Vedio-stitches-`
   - Standalone 9:16 vertical video clipper: FFmpeg rendering, Edge-TTS audio synthesis, subtitle burning, audio ducking.
   - No browser automation or Google Flow dependencies. Fully local, zero cloud required.
3. **MCP Creater (Tool Integrations)**: `c:\Users\gjaga\mcp creater`
   - Custom Model Context Protocol tools and servers.

## 2. Cross-Conversation Recall Rules
- Never treat any conversation as an isolated session.
- When the user asks about prior conversations, previous audits, research, or decisions made in other sessions, check `c:\Users\gjaga\jai\vault\antigravity_hub\CONVERSATION_HISTORY_INDEX.md` or execute `c:\Users\gjaga\jai\src\cross_conversation_memory.py`.
- You can inspect past transcripts directly at `C:\Users\gjaga\.gemini\antigravity\brain\<conversation_id>\.system_generated\logs\transcript.jsonl`.

## 3. Ground Truth Integrity
- Maintain full technical accuracy; never hallucinate character counts or video generation status.
- Keep all screenplay definitions, character anchors, and production assets synced in `c:\Users\gjaga\jai\vault\` and `c:\Users\gjaga\Downloads\Vedio-stitches-\clipmerge\config\`.

## 4. Mandatory Dual-Sided Red Flag Protocol (Checking Yours & Mine Every Time)
On every turn and action, the assistant and system must run this dual-sided safety check:

### 🚩 CHECKING THE ASSISTANT ("YOURS"):
1. **$0 Budget Hard Limit**: Strictly flag and prevent any tool or workflow requiring paid subscriptions or paid API credits. Default is always 100% free (Kaggle 30h/week GPUs, free Colab, open weights).
2. **VRAM Math Before Recommendation**: Never state a model runs on a specific GPU without checking exact memory math: `(Parameters * bits) / 8 * 1.25`. For 16GB GPUs, flag any model over 10B unless 4-bit quantized (GGUF/FP4).
3. **Verbatim Prompt Fidelity**: Never rewrite, summarize, or alter shot prompts. Exact user text must be passed to generators.
4. **Physical Asset Proof**: Never claim a video clip is complete without checking live screenshots or verifying non-zero bytes on disk.

### 🔴 CHECKING THE USER ("MINES"):
1. **Hardware Reality Guard**: If a requested workflow will crash the available hardware (e.g., OOM crash on Kaggle 16GB), immediately raise a red flag before execution.
2. **Missing Visual Anchors**: If a shot prompt lacks camera framing, lighting, or character references that will lead to floaty/melting AI outputs, flag it before wasting generation compute.
3. **Automated Validator Tool**: Execute `python c:\Users\gjaga\Downloads\Vedio-stitches-\clipmerge\red_flag_checker.py` to programmatically validate budget, VRAM limits, and prompt integrity.

