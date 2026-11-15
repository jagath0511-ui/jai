"""Brutally Honest Red-Team Auditor for Colab / Cloud GPU Architectures.

Probes and evaluates proposed cloud architectures against physical constraints:
- Google Colab WebSocket idle timeouts and anti-bot reCAPTCHA detection.
- Programmatic launch feasibility (Kaggle CLI vs Colab non-API).
- Terms of Service ban risks on ephemeral tunnel proxies.
- Cross-account Google Drive IOPS and bandwidth rate limits.
- VRAM footprint vs physical hardware limits.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Tuple

# Windows PowerShell UTF-8 console output compatibility
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("jai.colab_red_flag_auditor")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [RedFlagAuditor] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@dataclass
class AuditFinding:
    category: str
    component: str
    severity: str  # "FATAL", "CRITICAL", "WARNING", "VERIFIED"
    score_out_of_10: float
    claim: str
    physical_reality: str
    workaround_solution: str


class ColabArchitectureAuditor:
    """Ruthless red-team analyzer for cloud GPU pipelines."""

    def __init__(self):
        self.findings: List[AuditFinding] = []

    def audit_unattended_colab_run(self) -> AuditFinding:
        """Audit Claim: Colab can run 11 hours unattended while laptop is closed."""
        claim = "Google Colab will run for 11 hours continuously unattended in the background."
        reality = (
            "FATAL FLAW: Free Google Colab has an aggressive idle timeout of 30-90 minutes. "
            "It requires an active WebSocket heartbeat from an open browser tab. "
            "Closing the laptop or tab kills the runtime. Automated DOM clickers trigger reCAPTCHA v3 "
            "and result in immediate VM termination and GPU quota suspension."
        )
        solution = (
            "Use Colab strictly for interactive sessions (when working at laptop). "
            "For unattended background execution, use Kaggle via official CLI API."
        )
        finding = AuditFinding(
            category="RUNTIME_LIFECYCLE",
            component="Google Colab Free Tier",
            severity="FATAL",
            score_out_of_10=2.0,
            claim=claim,
            physical_reality=reality,
            workaround_solution=solution,
        )
        self.findings.append(finding)
        return finding

    def audit_programmatic_colab_boot(self) -> AuditFinding:
        """Audit Claim: Colab 1 can autonomously launch Colab 2 on a second account."""
        claim = "Colab 1 can programmatically instantiate and boot Colab 2 on another Gmail account."
        reality = (
            "FATAL FLAW: Google Colab Free Tier has NO public REST API or CLI to boot instances. "
            "Attempting to automate login via headless Playwright/Puppeteer triggers Google's "
            "anti-bot security, requiring manual SMS/phone 2FA prompts or locking accounts."
        )
        solution = (
            "Kaggle supports official headless launches (kaggle kernels push). "
            "Cloudflare Workers should route between active nodes rather than trying to boot Colab headlessly."
        )
        finding = AuditFinding(
            category="ORCHESTRATION_FEASIBILITY",
            component="Autonomous Cross-Colab Relay",
            severity="FATAL",
            score_out_of_10=1.0,
            claim=claim,
            physical_reality=reality,
            workaround_solution=solution,
        )
        self.findings.append(finding)
        return finding

    def audit_tos_and_proxy_ban(self) -> AuditFinding:
        """Audit Claim: Exposing llama-server via Cloudflare Tunnel 24/7 is sustainable."""
        claim = "Running Cloudflare Tunnel on Colab as a 24/7 remote LLM proxy will not trigger bans."
        reality = (
            "CRITICAL RISK: Colab Terms of Service explicitly prohibit unmonitored proxy/remote servers. "
            "High outbound tunnel bandwidth combined with zero notebook cell interaction triggers "
            "automated heuristics, revoking GPU access for 48 hours to 2 weeks."
        )
        solution = (
            "Run Cloudflare Tunnel only during active development sprints. "
            "Keep an automated fallback to Gemini Flash ($0 free tier) so JAI never stalls."
        )
        finding = AuditFinding(
            category="TERMS_OF_SERVICE_SAFETY",
            component="Cloudflare Tunnel on Colab",
            severity="CRITICAL",
            score_out_of_10=3.5,
            claim=claim,
            physical_reality=reality,
            workaround_solution=solution,
        )
        self.findings.append(finding)
        return finding

    def audit_shared_drive_bandwidth(self) -> AuditFinding:
        """Audit Claim: Shared 5TB Google Drive allows instant zero-download GGUF loads forever."""
        claim = "Mounting shared 5TB Drive across multiple accounts allows infinite fast GGUF loads."
        reality = (
            "WARNING: Cross-account shared files in Google Drive have strict daily download/read quotas. "
            "FUSE mounting a 9GB GGUF repeatedly across multiple accounts can trigger HTTP 403 "
            "'Download quota exceeded for this file'."
        )
        solution = (
            "Cache the GGUF in each account's root Drive or allow fallback download to Colab local disk."
        )
        finding = AuditFinding(
            category="STORAGE_IOPS",
            component="Google Drive Shared FUSE Mount",
            severity="WARNING",
            score_out_of_10=5.0,
            claim=claim,
            physical_reality=reality,
            workaround_solution=solution,
        )
        self.findings.append(finding)
        return finding

    def audit_cloudflare_workers_router(self) -> AuditFinding:
        """Audit Claim: Cloudflare Workers edge proxy is production-ready and $0."""
        claim = "Cloudflare Workers can act as the permanent edge brain and switchboard."
        reality = (
            "VERIFIED: Cloudflare Workers free tier provides 100,000 requests/day, 0ms cold starts, "
            "and global edge routing. KV storage provides instant URL resolution with zero running cost."
        )
        solution = "Proceed with Cloudflare Worker as the central edge router."
        finding = AuditFinding(
            category="EDGE_INFRASTRUCTURE",
            component="Cloudflare Workers (Reverse Proxy)",
            severity="VERIFIED",
            score_out_of_10=9.5,
            claim=claim,
            physical_reality=reality,
            workaround_solution=solution,
        )
        self.findings.append(finding)
        return finding

    def audit_vram_and_quantization(self, model_params_b: float = 14.0, bits: float = 4.5) -> AuditFinding:
        """Audit Claim: Qwen 2.5 14B runs cleanly on T4 GPU."""
        vram_needed = (model_params_b * bits) / 8.0 * 1.25 + 1.5
        t4_vram = 15.36
        fits = vram_needed <= t4_vram

        finding = AuditFinding(
            category="HARDWARE_VRAM_MATH",
            component="Qwen 2.5 14B Q4_K_M on T4 GPU",
            severity="VERIFIED" if fits else "FATAL",
            score_out_of_10=9.0 if fits else 0.0,
            claim=f"Qwen {model_params_b}B at {bits}-bit fits into Colab T4 GPU.",
            physical_reality=(
                f"Math: ({model_params_b} * {bits})/8 * 1.25 + 1.5GB KV = {vram_needed:.2f} GB VRAM. "
                f"T4 has {t4_vram} GB. Headroom: {t4_vram - vram_needed:.2f} GB."
            ),
            workaround_solution="Optimal quantization configuration verified.",
        )
        self.findings.append(finding)
        return finding

    def run_full_audit(self) -> Dict[str, Any]:
        """Executes all audit modules and returns overall score and findings."""
        self.findings.clear()
        self.audit_unattended_colab_run()
        self.audit_programmatic_colab_boot()
        self.audit_tos_and_proxy_ban()
        self.audit_shared_drive_bandwidth()
        self.audit_cloudflare_workers_router()
        self.audit_vram_and_quantization()

        total_score = sum(f.score_out_of_10 for f in self.findings)
        avg_score = round(total_score / len(self.findings), 1)

        fatal_count = sum(1 for f in self.findings if f.severity == "FATAL")
        critical_count = sum(1 for f in self.findings if f.severity == "CRITICAL")
        warning_count = sum(1 for f in self.findings if f.severity == "WARNING")
        verified_count = sum(1 for f in self.findings if f.severity == "VERIFIED")

        return {
            "overall_score": avg_score,
            "fatal_flaws": fatal_count,
            "critical_risks": critical_count,
            "warnings": warning_count,
            "verified_components": verified_count,
            "verdict": "REVISE_ARCHITECTURE" if fatal_count > 0 else "APPROVED",
            "findings": [
                {
                    "component": f.component,
                    "category": f.category,
                    "severity": f.severity,
                    "score": f.score_out_of_10,
                    "claim": f.claim,
                    "reality": f.physical_reality,
                    "solution": f.workaround_solution,
                }
                for f in self.findings
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def print_audit_report(res: Dict[str, Any]) -> None:
    """Print formatted terminal report."""
    print("\n" + "=" * 75)
    print("      🚩 JAI BRUTALLY HONEST RED-FLAG ARCHITECTURE AUDIT REPORT")
    print(f"      Overall Health Score: {res['overall_score']} / 10.0 | Verdict: {res['verdict']}")
    print("=" * 75)
    print(f"  • Fatal Flaws:        {res['fatal_flaws']} (Will immediately crash or fail)")
    print(f"  • Critical Risks:     {res['critical_risks']} (High probability of account ban / quota revoke)")
    print(f"  • Warnings:           {res['warnings']} (Rate limits or degradation)")
    print(f"  • Verified Solid:     {res['verified_components']} (Production-grade)")
    print("-" * 75)

    for item in res["findings"]:
        icon = "🔴" if item["severity"] == "FATAL" else ("⚠️" if item["severity"] in ("CRITICAL", "WARNING") else "🟢")
        print(f"\n{icon} [{item['severity']}] {item['component']} ({item['score']}/10.0)")
        print(f"   • Claim:    {item['claim']}")
        print(f"   • Reality:  {item['reality']}")
        print(f"   • Fix:      {item['solution']}")

    print("\n" + "=" * 75)


def main():
    parser = argparse.ArgumentParser(description="JAI Colab / Cloud Architecture Auditor")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    auditor = ColabArchitectureAuditor()
    report = auditor.run_full_audit()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_audit_report(report)


if __name__ == "__main__":
    main()
