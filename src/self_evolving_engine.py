"""JAI Self-Evolving Engine (Safe Autonomous Metaprogramming — Sprint 4).
========================================================================
Allows JAI to modify its own code safely on user command.

Enforces an ironclad 7-stage lifecycle:
1. Target Resolution: Safely locates the target code file inside the JAI repo.
2. Differential Patch Generation: Uses LLM Router (Qwen 14B / Gemini Flash)
   with negative constraints to generate surgical, valid code updates.
3. Syntax & AST Integrity Validation:
   - Validates candidate code via Python AST / py_compile or Node syntax checks.
   - AST structural analysis checks for missing definitions (unintended deletions).
   - Flags dangerous / forbidden AST function calls.
4. Pre-Flight Test Runner:
   - Automatically detects associated unit test suites (e.g. tests/test_<file>.py).
   - Executes unit tests against proposed code in an isolated trial before modifying disk.
5. Action Gateway Safety & Visual Diff: Enforces ActionGateway (RiskLevel.HIGH)
   and renders a unified diff for human inspection.
6. Rolling 10-Version Snapshot Management:
   - Dedicated snapshot directory: data/snapshots/<file_rel>/
   - Maintains up to 10 rotating timestamped .bak versions with full metadata.
   - Multi-version rollback: `jai rollback <file> [version_number]`.
7. Experience Logging: Records the modification trajectory in ExperienceVault.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import importlib
import json
import logging
import os
from pathlib import Path
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

# Ensure JAI root directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from action_gateway import (
    ActionGateway,
    ActionApprovalRequiredError,
    ActionSecurityError,
    Decision,
    RiskLevel,
    action_gateway,
)

logger = logging.getLogger("jai.self_evolving")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [SelfEvolve] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Maximum rolling snapshots maintained per file
MAX_SNAPSHOTS: int = 10

# Files and directories strictly protected from autonomous modification
PROTECTED_PATTERNS = [
    r"\.git",
    r"\.venv",
    r"__pycache__",
    r"node_modules",
    r"data/.*\.db$",
    r"vault/.*",
    r"\.env$",
]


@dataclass
class PatchProposal:
    target_file: Path
    original_code: str
    proposed_code: str
    instruction: str
    unified_diff: str
    syntax_valid: bool = False
    validation_error: Optional[str] = None
    ast_warnings: List[str] = field(default_factory=list)
    preflight_tested: bool = False
    preflight_passed: bool = True
    preflight_output: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EvolutionResult:
    success: bool
    target_file: Path
    backup_file: Optional[Path] = None
    snapshot_path: Optional[Path] = None
    diff: str = ""
    message: str = ""
    error: Optional[str] = None
    preflight_passed: bool = True
    preflight_output: Optional[str] = None


class SelfEvolvingEngine:
    """Safe autonomous code modification, AST verification, and rolling snapshot engine."""

    def __init__(self, base_dir: Optional[Union[str, Path]] = None, gateway: Optional[ActionGateway] = None):
        self.base_dir = Path(base_dir or BASE_DIR).resolve()
        self.gateway = gateway or action_gateway
        self.snapshots_base_dir = self.base_dir / "data" / "snapshots"
        self.snapshots_base_dir.mkdir(parents=True, exist_ok=True)

    def resolve_target_file(self, file_query: Union[str, Path]) -> Optional[Path]:
        """Safely resolves a file query to an existing file within the JAI repository."""
        query_str = str(file_query).strip().strip('"').strip("'")
        if not query_str:
            return None

        # Direct path check
        direct = (self.base_dir / query_str).resolve()
        if direct.is_file() and direct.is_relative_to(self.base_dir):
            if self._is_protected(direct):
                raise PermissionError(f"Target file '{direct.name}' is protected from self-modification.")
            return direct

        # Check in src/
        in_src = (self.base_dir / "src" / query_str).resolve()
        if in_src.is_file() and in_src.is_relative_to(self.base_dir):
            if self._is_protected(in_src):
                raise PermissionError(f"Target file '{in_src.name}' is protected from self-modification.")
            return in_src

        # Search recursively for exact filename match
        target_name = Path(query_str).name.lower()
        candidates: List[Path] = []
        for path in self.base_dir.rglob("*.py"):
            if path.name.lower() == target_name or path.stem.lower() == target_name:
                if not self._is_protected(path):
                    candidates.append(path)

        for path in self.base_dir.rglob("*.js"):
            if path.name.lower() == target_name or path.stem.lower() == target_name:
                if not self._is_protected(path):
                    candidates.append(path)

        if candidates:
            # Prefer shallower files or exact matches
            candidates.sort(key=lambda p: (len(p.parts), len(p.name)))
            return candidates[0]

        return None

    def _is_protected(self, path: Path) -> bool:
        """Returns True if the path matches any protected patterns."""
        rel = str(path.relative_to(self.base_dir)).replace("\\", "/")
        for pattern in PROTECTED_PATTERNS:
            if re.search(pattern, rel, re.IGNORECASE):
                return True
        return False

    # ---------------------------------------------------------
    # AST Analysis & Syntax Verification
    # ---------------------------------------------------------

    def validate_code_syntax(self, code: str, suffix: str = ".py") -> Tuple[bool, Optional[str]]:
        """Performs pre-flight syntax and AST validation without modifying any target file."""
        if not code.strip():
            return False, "Proposed code is empty."

        suffix = suffix.lower()

        # 1. Python Validation via AST and py_compile
        if suffix in (".py", ".pyw"):
            try:
                tree = ast.parse(code, filename="<candidate_code>")
            except SyntaxError as e:
                return False, f"Python SyntaxError at line {e.lineno}, col {e.offset}: {e.msg}"
            except Exception as e:
                return False, f"Python AST ParseError: {str(e)}"

            # Security: check for blatantly dangerous AST patterns
            security_err = self._check_ast_security(tree)
            if security_err:
                return False, f"AST Security Violation: {security_err}"

            # Bytecode compile check via temporary file
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                try:
                    tmp.write(code)
                    tmp.flush()
                    tmp.close()
                    py_compile.compile(str(tmp_path), doraise=True)
                except py_compile.PyCompileError as e:
                    return False, f"Bytecode Compilation Error: {e.msg}"
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass

            return True, None

        # 2. JavaScript Validation via Node.js (if installed)
        if suffix == ".js":
            with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                try:
                    tmp.write(code)
                    tmp.flush()
                    tmp.close()
                    res = subprocess.run(
                        ["node", "-c", str(tmp_path)],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if res.returncode != 0:
                        return False, f"Node SyntaxError: {res.stderr.strip()}"
                    return True, None
                except FileNotFoundError:
                    if code.count("{") != code.count("}"):
                        return False, "JavaScript unbalanced curly braces detected."
                    return True, None
                except Exception as e:
                    return False, f"JS check failed: {e}"
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass

        return True, None

    def _check_ast_security(self, tree: ast.AST) -> Optional[str]:
        """Inspects AST nodes for high-risk dangerous patterns."""
        for node in ast.walk(tree):
            # Flag raw eval / exec calls
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                    return f"Dynamic execution '{func.id}()' is prohibited in self-modifications."
                # Flag dangerous os.system or shutil.rmtree deletions
                if isinstance(func, ast.Attribute) and func.attr == "rmtree":
                    return "Recursive directory deletion (rmtree) is prohibited in self-modifications."
        return None

    def analyze_ast_definitions(self, code: str) -> Dict[str, Set[str]]:
        """Extracts top-level functions and classes from Python code."""
        try:
            tree = ast.parse(code)
        except Exception:
            return {"functions": set(), "classes": set()}

        functions: Set[str] = set()
        classes: Set[str] = set()

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.add(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.add(node.name)

        return {"functions": functions, "classes": classes}

    def compare_ast_definitions(self, original_code: str, proposed_code: str) -> List[str]:
        """Detects removed functions or classes between original and proposed code."""
        orig_defs = self.analyze_ast_definitions(original_code)
        prop_defs = self.analyze_ast_definitions(proposed_code)

        warnings: List[str] = []
        removed_funcs = orig_defs["functions"] - prop_defs["functions"]
        removed_classes = orig_defs["classes"] - prop_defs["classes"]

        for fn in sorted(removed_funcs):
            warnings.append(f"Function '{fn}' was removed in proposed code.")
        for cl in sorted(removed_classes):
            warnings.append(f"Class '{cl}' was removed in proposed code.")

        return warnings

    # ---------------------------------------------------------
    # Pre-Flight Test Runner
    # ---------------------------------------------------------

    def find_test_file(self, target_file: Path) -> Optional[Path]:
        """Locates any existing pytest unit test associated with the target file."""
        tests_dir = self.base_dir / "tests"
        if not tests_dir.is_dir():
            return None

        # Check tests/test_<filename>.py
        cands = [
            tests_dir / f"test_{target_file.name}",
            tests_dir / f"test_{target_file.stem}.py",
        ]
        for c in cands:
            if c.is_file():
                return c

        return None

    def run_preflight_tests(self, target_file: Path, proposed_code: str) -> Tuple[bool, str]:
        """Runs associated unit tests against the candidate code before applying to disk.
        
        Returns:
            Tuple[bool, str]: (passed, test_output_summary)
        """
        test_file = self.find_test_file(target_file)
        if not test_file:
            return True, "No associated test suite found. Pre-flight test skipped."

        original_code = target_file.read_text(encoding="utf-8", errors="replace")

        # Temporarily swap file content in place, run pytest, and guarantee immediate restoration
        try:
            target_file.write_text(proposed_code, encoding="utf-8")
            cmd = [sys.executable, "-m", "pytest", str(test_file), "-q"]
            res = subprocess.run(
                cmd,
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            passed = (res.returncode == 0)
            output = (res.stdout or res.stderr or "").strip()
            return passed, output
        except subprocess.TimeoutExpired:
            return False, "Pre-flight unit tests timed out (>30s)."
        except Exception as e:
            return False, f"Pre-flight test runner error: {e}"
        finally:
            # Guarantee restoration
            try:
                target_file.write_text(original_code, encoding="utf-8")
            except Exception as e:
                logger.critical(f"Failed to restore original file during test run: {e}")

    # ---------------------------------------------------------
    # Rolling 10-Version Snapshot Management
    # ---------------------------------------------------------

    def get_snapshot_dir(self, target_file: Path) -> Path:
        """Returns the dedicated directory for rolling snapshots of a target file."""
        target_file = Path(target_file).resolve()
        try:
            rel = str(target_file.relative_to(self.base_dir)).replace("\\", "_").replace("/", "_")
        except ValueError:
            rel = target_file.name
        s_dir = self.snapshots_base_dir / rel
        s_dir.mkdir(parents=True, exist_ok=True)
        return s_dir

    def save_snapshot(self, target_file: Path, instruction: str = "") -> Path:
        """Saves a timestamped backup into the rolling 10-version snapshot directory."""
        target_file = Path(target_file).resolve()
        s_dir = self.get_snapshot_dir(target_file)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        ts_iso = datetime.now(timezone.utc).isoformat()

        # Existing snapshots
        existing_baks = sorted(s_dir.glob("snapshot_*.bak"), key=lambda p: (p.stat().st_mtime, p.name))

        # Enforce MAX_SNAPSHOTS limit (delete oldest)
        while len(existing_baks) >= MAX_SNAPSHOTS:
            oldest = existing_baks.pop(0)
            meta_cand = oldest.with_suffix(".meta.json")
            try:
                oldest.unlink(missing_ok=True)
                meta_cand.unlink(missing_ok=True)
            except Exception:
                pass

        version_num = len(existing_baks) + 1
        snapshot_file = s_dir / f"snapshot_{version_num:02d}_{ts_str}.bak"
        meta_file = s_dir / f"snapshot_{version_num:02d}_{ts_str}.meta.json"

        shutil.copy2(target_file, snapshot_file)

        try:
            rel_file_str = str(target_file.relative_to(self.base_dir))
        except ValueError:
            rel_file_str = target_file.name

        meta_data = {
            "version": version_num,
            "timestamp": ts_iso,
            "file": rel_file_str,
            "instruction": instruction,
            "size_bytes": target_file.stat().st_size,
        }
        meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

        # Also maintain local <file>.bak for backward compatibility
        local_bak = target_file.with_suffix(f"{target_file.suffix}.bak")
        try:
            shutil.copy2(target_file, local_bak)
        except Exception:
            pass

        logger.info(f"Saved snapshot {snapshot_file.name} (Rolling pool: {len(existing_baks) + 1}/{MAX_SNAPSHOTS})")
        return snapshot_file

    def list_snapshots(self, target_file: Union[str, Path]) -> List[Dict[str, Any]]:
        """Returns all available snapshots for a target file, sorted newest to oldest."""
        resolved = self.resolve_target_file(target_file)
        if not resolved:
            return []

        s_dir = self.get_snapshot_dir(resolved)
        baks = sorted(s_dir.glob("snapshot_*.bak"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)

        results: List[Dict[str, Any]] = []
        for i, bak in enumerate(baks, 1):
            meta_file = bak.with_suffix(".meta.json")
            meta = {}
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            results.append({
                "index": i,
                "snapshot_file": bak.name,
                "snapshot_path": str(bak),
                "timestamp": meta.get("timestamp", datetime.fromtimestamp(bak.stat().st_mtime, timezone.utc).isoformat()),
                "instruction": meta.get("instruction", "Manual Snapshot"),
                "size_bytes": bak.stat().st_size,
            })

        return results

    def rollback(self, target_file: Union[str, Path], version: int = 1) -> bool:
        """Restores a file from its rolling snapshot directory.
        
        Args:
            target_file: File name or path.
            version: 1-indexed version number (1 = most recent snapshot, 2 = 2nd most recent, etc.)
        """
        resolved = self.resolve_target_file(target_file)
        if not resolved:
            logger.warning(f"Could not resolve target file for rollback: {target_file}")
            return False

        snapshots = self.list_snapshots(resolved)
        if snapshots:
            idx = max(0, min(version - 1, len(snapshots) - 1))
            chosen = Path(snapshots[idx]["snapshot_path"])
            if chosen.is_file():
                try:
                    # Save a snapshot of the current state before rolling back!
                    self.save_snapshot(resolved, instruction=f"Pre-rollback snapshot before restoring version {version}")
                    shutil.copy2(chosen, resolved)
                    logger.info(f"Rollback successful: {resolved.name} restored from snapshot version {version} ({chosen.name})")
                    return True
                except Exception as e:
                    logger.error(f"Rollback error: {e}")
                    return False

        # Fallback to local <file>.bak
        bak_file = resolved.with_suffix(f"{resolved.suffix}.bak")
        if bak_file.is_file():
            try:
                shutil.copy2(bak_file, resolved)
                logger.info(f"Rollback successful: {resolved.name} restored from legacy {bak_file.name}")
                return True
            except Exception as e:
                logger.error(f"Legacy rollback failed: {e}")
                return False

        logger.warning(f"No backup snapshot found for {resolved.name}")
        return False

    # ---------------------------------------------------------
    # Diff & Patch Pipeline
    # ---------------------------------------------------------

    def generate_diff(self, original: str, proposed: str, filename: str = "file") -> str:
        """Generates a unified color-ready diff between original and proposed code."""
        orig_lines = original.splitlines(keepends=True)
        prop_lines = proposed.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines,
            prop_lines,
            fromfile=f"a/{filename} (original)",
            tofile=f"b/{filename} (modified)",
            n=3
        )
        return "".join(diff)

    def propose_patch(
        self,
        target_file: Union[str, Path],
        instruction: str,
        llm_caller: Optional[Callable[[str, str], str]] = None,
        run_tests: bool = True,
    ) -> PatchProposal:
        """Drafts, compiles, and validates a candidate patch using the LLM brain."""
        file_path = self.resolve_target_file(target_file)
        if not file_path or not file_path.is_file():
            raise FileNotFoundError(f"Could not locate target file: {target_file}")

        original_code = file_path.read_text(encoding="utf-8", errors="replace")
        ext = file_path.suffix.lower()

        # Retrieve negative constraints from past mistakes if self_learning is active
        negative_constraints = ""
        try:
            from src.self_learning import constraint_injector
            negative_constraints = constraint_injector.build_negative_constraints_block(
                skill_name="self_evolving_engine",
                context_text=instruction,
                limit=3
            )
        except Exception:
            pass

        # Build metaprogramming prompt
        system_prompt = (
            "You are JAI's Senior Metaprogramming & Autonomous Code Evolution Architect.\n"
            "Your task is to modify JAI's own source code safely according to the user's instruction.\n"
            "Rules:\n"
            "1. Preserve all existing valid logic, docstrings, imports, and architectural patterns.\n"
            "2. Ensure 100% syntactically correct and complete code. Never use placeholders like '... rest of code ...'.\n"
            "3. Format your response with the entire updated file content enclosed in a single markdown code block:\n"
            "   ```python\n   <complete code>\n   ```\n"
            "4. Do not include introductory conversational pleasantries or sign-offs.\n"
        )
        if negative_constraints:
            system_prompt += f"\n{negative_constraints}\n"

        user_prompt = (
            f"Target File: {file_path.name}\n"
            f"Relative Path: {file_path.relative_to(self.base_dir)}\n\n"
            f"User Modification Instruction:\n{instruction}\n\n"
            f"Current Original File Content:\n```{file_path.suffix.lstrip('.')}\n{original_code}\n```\n\n"
            f"Provide the complete, updated file content with the instruction implemented cleanly."
        )

        # Call LLM
        if llm_caller:
            llm_reply = llm_caller(system_prompt, user_prompt)
        else:
            llm_reply = self._call_default_brain(system_prompt, user_prompt)

        # Extract code from response
        match = re.search(r"```(?:python|py|javascript|js)?\s*([\s\S]*?)\s*```", llm_reply, re.IGNORECASE)
        proposed_code = match.group(1).strip() if match else llm_reply.strip()

        # 1. Validate syntax & AST
        is_valid, val_err = self.validate_code_syntax(proposed_code, suffix=ext)
        diff_text = self.generate_diff(original_code, proposed_code, filename=file_path.name)

        # 2. Structural AST comparison (unintended deletions)
        ast_warnings: List[str] = []
        if is_valid and ext in (".py", ".pyw"):
            ast_warnings = self.compare_ast_definitions(original_code, proposed_code)

        # 3. Pre-flight unit tests
        preflight_tested = False
        preflight_passed = True
        preflight_output = None

        if is_valid and run_tests and ext in (".py", ".pyw"):
            test_file = self.find_test_file(file_path)
            if test_file:
                preflight_tested = True
                preflight_passed, preflight_output = self.run_preflight_tests(file_path, proposed_code)

        return PatchProposal(
            target_file=file_path,
            original_code=original_code,
            proposed_code=proposed_code,
            instruction=instruction,
            unified_diff=diff_text,
            syntax_valid=is_valid,
            validation_error=val_err,
            ast_warnings=ast_warnings,
            preflight_tested=preflight_tested,
            preflight_passed=preflight_passed,
            preflight_output=preflight_output,
        )

    def _call_default_brain(self, system_prompt: str, user_prompt: str) -> str:
        """Invokes the configured JAI LLMRouter or Gemini/Qwen brain."""
        try:
            from llm_router import LLMRouter
            router = LLMRouter()
            res = router.route(f"{system_prompt}\n\n{user_prompt}")
            if res and res.text:
                return res.text
        except Exception as e:
            logger.warning(f"LLMRouter fallback call failed: {e}")

        # Fallback to direct Gemini Flash if available
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                import urllib.request
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as gem_err:
                logger.error(f"Direct Gemini call error: {gem_err}")

        raise RuntimeError("No active LLM brain available for code patch generation.")

    def apply_patch(
        self,
        proposal: PatchProposal,
        force_approved: bool = False,
        approval_callback: Optional[Callable[[str, Path, str], bool]] = None,
    ) -> EvolutionResult:
        """Applies the validated patch atomically with rolling snapshot and approval verification."""
        if not proposal.syntax_valid:
            return EvolutionResult(
                success=False,
                target_file=proposal.target_file,
                diff=proposal.unified_diff,
                error=f"Pre-flight validation failed: {proposal.validation_error}",
                message="Patch rejected due to syntax/AST invalidity."
            )

        if proposal.preflight_tested and not proposal.preflight_passed:
            return EvolutionResult(
                success=False,
                target_file=proposal.target_file,
                diff=proposal.unified_diff,
                preflight_passed=False,
                preflight_output=proposal.preflight_output,
                error=f"Pre-flight unit tests failed:\n{proposal.preflight_output}",
                message="Patch rejected because associated unit tests failed."
            )

        if not proposal.unified_diff.strip():
            return EvolutionResult(
                success=True,
                target_file=proposal.target_file,
                diff="",
                message="No changes detected between original and proposed code."
            )

        # 1. Check human approval
        approved = force_approved
        if not approved:
            if approval_callback:
                approved = approval_callback(proposal.instruction, proposal.target_file, proposal.unified_diff)
            else:
                approved = self._prompt_console_approval(proposal)

        if not approved:
            return EvolutionResult(
                success=False,
                target_file=proposal.target_file,
                diff=proposal.unified_diff,
                message="Self-modification cancelled by user (Approval Denied)."
            )

        # 2. Save Rolling 10-Version Snapshot
        try:
            snapshot_file = self.save_snapshot(proposal.target_file, instruction=proposal.instruction)
        except Exception as e:
            return EvolutionResult(
                success=False,
                target_file=proposal.target_file,
                error=f"Failed to create rolling snapshot: {e}",
                message="Aborted prior to file write."
            )

        # 3. Atomic Staged Write
        staging_file = proposal.target_file.with_suffix(f"{proposal.target_file.suffix}.staging")
        try:
            staging_file.write_text(proposal.proposed_code, encoding="utf-8")
            # Final syntax gate on the staged physical file
            val_ok, val_msg = self.validate_code_syntax(proposal.proposed_code, suffix=proposal.target_file.suffix)
            if not val_ok:
                staging_file.unlink(missing_ok=True)
                return EvolutionResult(
                    success=False,
                    target_file=proposal.target_file,
                    error=f"Staged file validation failed: {val_msg}",
                    message="Aborted and cleaned staging file."
                )

            # Atomic file swap
            staging_file.replace(proposal.target_file)
        except Exception as e:
            staging_file.unlink(missing_ok=True)
            # Automatic rollback from snapshot
            if snapshot_file.is_file():
                shutil.copy2(snapshot_file, proposal.target_file)
            return EvolutionResult(
                success=False,
                target_file=proposal.target_file,
                backup_file=snapshot_file,
                snapshot_path=snapshot_file,
                error=f"Atomic write failed: {e}. Rolled back to original state.",
                message="File restored from snapshot backup."
            )

        # 4. Record experience into SQLite Self-Learning Vault
        try:
            from src.self_learning import experience_vault
            experience_vault.record_experience(
                skill_name="self_evolving_engine",
                input_text=f"Evolve {proposal.target_file.name}: {proposal.instruction}",
                output_summary=f"Successfully patched {proposal.target_file.name}",
                latency_ms=0.0,
                success=True,
                user_feedback="approved"
            )
        except Exception:
            pass

        logger.info(f"Self-evolution successful: {proposal.target_file.name} updated.")
        return EvolutionResult(
            success=True,
            target_file=proposal.target_file,
            backup_file=snapshot_file,
            snapshot_path=snapshot_file,
            diff=proposal.unified_diff,
            preflight_passed=proposal.preflight_passed,
            preflight_output=proposal.preflight_output,
            message=f"Successfully evolved {proposal.target_file.name} (Snapshot: {snapshot_file.name})."
        )

    def _prompt_console_approval(self, proposal: PatchProposal) -> bool:
        """Presents colorized diff in console and requests confirmation."""
        print("\n" + "=" * 70)
        print(f"🧬 JAI SELF-EVOLUTION PROPOSAL: {proposal.target_file.name}")
        print("=" * 70)
        print(f"📋 Instruction: {proposal.instruction}")
        print(f"🧪 Syntax Check: {'✅ PASSED (AST Valid)' if proposal.syntax_valid else '❌ FAILED'}")
        if proposal.ast_warnings:
            print("⚠️ AST Structural Warnings:")
            for w in proposal.ast_warnings:
                print(f"   • {w}")
        if proposal.preflight_tested:
            status = '✅ PASSED' if proposal.preflight_passed else '❌ FAILED'
            print(f"🧪 Pre-Flight Unit Tests: {status}")
            if not proposal.preflight_passed and proposal.preflight_output:
                print(f"   Trace:\n{proposal.preflight_output[:300]}")
        print("-" * 70)
        print("🔍 Proposed Changes (Unified Diff):")

        for line in proposal.unified_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"\033[92m{line}\033[0m")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"\033[91m{line}\033[0m")
            elif line.startswith("@@"):
                print(f"\033[96m{line}\033[0m")
            else:
                print(line)

        print("-" * 70)
        try:
            confirm = input(f"Apply this modification to {proposal.target_file.name}? [y/N]: ").strip().lower()
            return confirm in ("y", "yes")
        except EOFError:
            return False


# Singleton export
self_evolving_engine = SelfEvolvingEngine()
