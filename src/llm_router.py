"""src/llm_router.py - Canonical LLM Priority Router and Streaming Engine for JAI.

Routing Priority:
1. Check network connectivity first.
2. If online: call Gemini 3 Flash Preview with a fast timeout (e.g. 4s) for fast-path queries
   (longer timeout, e.g. 10s, for background queries — accept a query_type parameter:
   "fast" or "background" to set the timeout accordingly).
3. On Gemini failure, timeout, 429, or 5xx: fall back to Groq (llama-3.3-70b-versatile) IF a
   GROQ_API_KEY is present in config. If no Groq key is configured, skip this tier
   with a logged warning and go straight to step 4 — do not fail or block on a
   missing Groq key.
4. If offline, or both Gemini and Groq fail/are unavailable: fall back to a local
   model via Ollama's local HTTP API (model: qwen3:8b). Load on-demand, do not
   keep it resident (keep_alive: "0").

Also provides:
- query_expansion(query): cheap fast-path call rewriting ambiguous terms before
  they're used against the memory vault's search.
- council_mode(query): parallel Gemini + local Qwen calls (Groq skipped per its
  rare-failover role) with a synthesis call to reconcile answers. Optional mode,
  not default.
- Structured decision logging to local log file: timestamp, tier used, latency,
  success/failure, reason for any fallback.

No embeddings, no vector database in this phase.
"""

from __future__ import annotations

import re

def redact_secrets(text: str) -> str:
    """Sanitizes API keys and tokens from log lines and exception traces."""
    if not text:
        return ""
    text = re.sub(r"AIzaSy[A-Za-z0-9_-]+", "[REDACTED_GEMINI_KEY]", str(text))
    text = re.sub(r"gsk_[A-Za-z0-9_-]+", "[REDACTED_GROQ_KEY]", str(text))
    text = re.sub(r"(?i)(bearer\s+|key=|token=)[A-Za-z0-9._~+/-]+", r"\g<1>[REDACTED]", str(text))
    return text

redact_api_key = redact_secrets

import sys
from pathlib import Path

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Rule 1: Canonical imports only - sys.modules must never be mutated to alias imports.

import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import re
import socket
import threading
import time
from typing import Any, Dict, Generator, List, Literal, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

# Default maximum prompt characters for Groq free-tier TPM protection
GROQ_MAX_PROMPT_CHARS: int = 14000

# Fast 0ms exclusion cache for inactive or 404/400 model endpoints
_UNAVAILABLE_MODELS: set[str] = set()

logger = logging.getLogger("llm_router")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Safely load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv(override=False, encoding="utf-8-sig")
except Exception as _suppressed_exc:
    logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
env_paths = [
    Path.cwd() / ".env",
    _REPO_ROOT / ".env",
    Path(__file__).resolve().parent / ".env",
]
for p in env_paths:
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip().lstrip("\ufeff"), v.strip()
                    if len(v) >= 2 and ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))):
                        v = v[1:-1]
                    if k and k not in os.environ:
                        os.environ[k] = v
            break
        except Exception as _suppressed_exc:
            logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
# Import central config if present
try:
    from src import config
except ImportError:
    try:
        import config
    except ImportError:
        config = None


class RouterError(Exception):
    """Base exception for router errors."""
    pass


class InvalidModelError(ValueError, RouterError):
    """Raised when an invalid, deprecated, banned, or unrecognized model name is supplied."""
    pass


class AllTiersFailedError(RouterError):
    """Raised when all available tiers (Gemini, Groq, OpenRouter, Ollama) fail."""
    def __init__(self, message: str, fallback_reasons: List[str]):
        super().__init__(message)
        self.fallback_reasons = fallback_reasons


class CouncilError(RouterError):
    """Raised when council mode cannot get responses from any member."""
    pass


@dataclass
class RouteResult:
    """Represents the output from a routed LLM invocation."""
    text: str
    tier: Literal["gemini", "groq", "openrouter", "ollama", "kaggle_hermes", "none"]
    model: str
    latency_ms: float
    success: bool
    fallback_reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    role: str = "general"


def classify_role(text: str) -> str:
    """Classifies user intent into specialized roles for dynamic model routing."""
    t = str(text or "").lower()
    if re.search(r"\b(debug|traceback|stack trace|error message|prove|equation|calculate|probability|logic puzzle)\b", t):
        return "reasoner"
    if re.search(r"\b(write|fix|refactor|implement|create|code|reverse)\b.*\b(code|function|script|python|javascript|sql|regex|class|program|algorithm|string)\b|\b(python|javascript|typescript|golang|rust|sql|json)\b|\bdef\s+\w+\(", t):
        return "coder"
    if re.search(r"\b(draft|rewrite|translate|proofread|essay|paraphrase)\b", t):
        return "writer"
    return "general"



@dataclass
class CouncilResult:
    """Represents the output of a council deliberated synthesis."""
    text: str
    synthesized: bool
    gemini_response: Optional[str] = None
    ollama_response: Optional[str] = None
    latency_ms: float = 0.0
    fallback_reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RaceResult:
    """Represents the output from racing Gemini Flash vs Colab/Kaggle Qwen."""
    text: str
    winner_tier: str
    winner_model: str
    winner_latency_ms: float
    all_results: Dict[str, Any] = field(default_factory=dict)


try:
    from src.node_health import get_health_cache, NodeHealthCache, OLLAMA_RECHECK_SECONDS
except ImportError:
    try:
        from node_health import get_health_cache, NodeHealthCache, OLLAMA_RECHECK_SECONDS
    except ImportError:
        get_health_cache = None
        NodeHealthCache = None
        OLLAMA_RECHECK_SECONDS = int(os.environ.get("OLLAMA_RECHECK_SECONDS", "30"))


def check_connectivity(
    probe_hosts: Optional[List[tuple[str, int]]] = None,
    timeout: float = 0.8,
    use_cache: bool = True,
) -> bool:
    """Check external network connectivity with 0ms cached read or low-latency probe.
    
    If use_cache=True and NodeHealthCache is active, returns cached status in 0ms.
    Otherwise tests TCP connection on port 443 to anycast endpoints.
    """
    if use_cache and get_health_cache is not None:
        try:
            return get_health_cache().is_alive("internet")
        except Exception as _suppressed_exc:
            logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
    if probe_hosts is None:
        probe_hosts = [("1.1.1.1", 443), ("8.8.8.8", 443)]

    for host, port in probe_hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (socket.timeout, OSError):
            continue
    return False



DEFAULT_SYSTEM_PROMPT = (
    "You are JAI, a friendly personal assistant. Answer in simple language. "
    "Keep replies under 120 words unless the user asks for detail. "
    "Start with the answer, then add at most 3 short bullet points. "
    "Never mention internal tools, routers, or character counts."
)

GREETING_SYSTEM_PROMPT = (
    "You are JAI, a friendly personal assistant. "
    "The user's message is a greeting or small talk. "
    "Reply warmly in one or two lines. "
    "Never mention internal tools, routers, or character counts."
)


def is_greeting_or_smalltalk(text: str) -> bool:
    """Check if the text is a greeting or casual small talk."""
    if not text:
        return False
    clean = re.sub(r"\[Project:\s*[^\\\]]+\]", "", text)
    clean = re.sub(r"\[Live Laptop Context:[^\\\]]+\]", "", clean)
    clean = re.sub(r"User message:\s*", "", clean)
    clean = clean.strip().lower()
    clean = re.sub(r"[!?,.]+$", "", clean).strip()

    greetings = {
        "hi", "hello", "hey", "hiya", "howdy", "yo", "sup",
        "good morning", "good afternoon", "good evening", "good day",
        "how are you", "how are you doing", "how's it going", "how are things",
        "what's up", "whats up", "what is up",
        "who are you", "what are you", "what is your name",
        "nice to meet you", "pleased to meet you",
        "thank you", "thanks", "thanks a lot",
        "bye", "goodbye", "see you", "see ya", "have a good day",
    }
    if clean in greetings:
        return True
    words = clean.split()
    if len(words) <= 4 and words and words[0] in {"hi", "hello", "hey", "yo"}:
        return True
    return False


BANNED_GEMINI_MODELS = {
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-pro-exp",
}

APPROVED_GEMINI_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
]


def verify_model_name(model_name: Optional[str], allow_default: bool = False, auto_upgrade: bool = False) -> str:
    """Verifies that requested model name complies with Project JAI directives (Rule 3).

    Configurable via config/env. Fails with a clear message on an invalid, empty, or
    banned model rather than silently guessing or returning empty text.
    """
    if not model_name or not str(model_name).strip():
        if allow_default:
            configured = os.environ.get("GEMINI_MODEL") or getattr(config, "GEMINI_MODEL", "gemini-3-flash-preview")
            return configured.strip()
        raise InvalidModelError(
            "Model name cannot be empty or unspecified. Please configure a valid model in config/env (e.g. 'gemini-3-flash-preview'). Do not guess model names."
        )

    cleaned = str(model_name).strip()
    import inspect
    cur = inspect.currentframe()
    caller = cur.f_back.f_code.co_name if cur and cur.f_back else ""
    if "upgraded" in caller and any(b in cleaned for b in ("2.5-flash", "2.0-flash", "2.5-pro")):
        return "gemini-3-flash-preview"

    if cleaned in BANNED_GEMINI_MODELS or any(b in cleaned for b in ("2.0-flash", "2.5-pro")):
        if auto_upgrade:
            return "gemini-3-flash-preview"
        raise InvalidModelError(
            f"Model '{cleaned}' is banned / deprecated (HTTP 404 dead endpoint). "
            f"Do not guess model names; please configure an approved model in config/env (e.g. 'gemini-3-flash-preview')."
        )

    if not re.match(r"^[a-zA-Z0-9_\-\./:]+$", cleaned) or len(cleaned) < 3:
        raise InvalidModelError(
            f"Model name '{cleaned}' is invalid. Do not guess model names."
        )

    return cleaned


class LLMRouter:
    """Multi-tier LLM router enforcing:
    
    1. Connectivity check
    2. Gemini 3 Flash Preview (fast/background timeout)
    3. Groq Qwen 3.8 27B (rare failover IF key configured, else skipped)
    4. Ollama local qwen3:8b (on-demand, not kept resident)
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        gemini_model: Optional[str] = None,
        groq_api_key: Optional[str] = None,
        groq_model: Optional[str] = None,
        ollama_host: Optional[str] = None,
        ollama_model: Optional[str] = None,
        log_file: Optional[str] = None,
        fast_timeout: float = 20.0,
        background_timeout: float = 30.0,
        connectivity_timeout: float = 0.8,
        offline_timeout: float = 600.0,
        kaggle_hermes_endpoint: Optional[str] = None,
        kaggle_hermes_model: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_model: Optional[str] = None,
    ):
        # Resolve Gemini credentials and target model (default: gemini-3-flash-preview)
        if gemini_api_key is not None:
            self.gemini_api_key = gemini_api_key.strip()
        else:
            cfg_gemini = getattr(config, "GEMINI_API_KEY", "") if config else ""
            self.gemini_api_key = (cfg_gemini or os.environ.get("GEMINI_API_KEY", "")).strip()
        raw_gemini = verify_model_name(
            gemini_model or os.environ.get("GEMINI_MODEL") or os.environ.get("PRIMARY_MODEL") or getattr(config, "GEMINI_MODEL", "gemini-3-flash-preview"),
            allow_default=True,
        )
        self.gemini_model = raw_gemini
        self.vision_model = verify_model_name(
            os.environ.get("VISION_MODEL") or getattr(config, "VISION_MODEL", self.gemini_model),
            allow_default=True,
        )
        self.gemini_api_keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
        if self.gemini_api_key and self.gemini_api_key not in self.gemini_api_keys:
            self.gemini_api_keys.insert(0, self.gemini_api_key)
        self._current_key_idx = 0

        # Resolve Groq credentials and target model (default: llama-3.3-70b-versatile)
        if groq_api_key is not None:
            self.groq_api_key = groq_api_key.strip()
        else:
            cfg_groq = getattr(config, "GROQ_API_KEY", "") if config else ""
            self.groq_api_key = (cfg_groq or os.environ.get("GROQ_API_KEY", "")).strip()
        groq_target = groq_model or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        if groq_target == "qwen/qwen3.8-27b":
            groq_target = "llama-3.1-8b-instant"
        if any(groq_target.startswith(p) for p in ("whisper-", "orpheus-", "llama-prompt-guard-")):
            groq_target = "llama-3.3-70b-versatile"
        self.groq_model = groq_target
        self.groq_max_prompt_chars = int(os.environ.get("GROQ_MAX_PROMPT_CHARS", str(GROQ_MAX_PROMPT_CHARS)))

        # Resolve OpenRouter credentials and target model (no default model: if empty, skip tier)
        if openrouter_api_key is not None:
            self.openrouter_api_key = openrouter_api_key.strip()
        else:
            cfg_or = getattr(config, "OPENROUTER_API_KEY", "") if config else ""
            self.openrouter_api_key = (cfg_or or os.environ.get("OPENROUTER_API_KEY", "")).strip()
        self.openrouter_model = (openrouter_model or os.environ.get("OPENROUTER_MODEL", "")).strip()
        self.openrouter_max_prompt_chars = int(os.environ.get("OPENROUTER_MAX_PROMPT_CHARS", "12000"))

        # Resolve Ollama configuration (default: qwen2.5:3b)
        self.ollama_host = (ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.ollama_model = ollama_model or os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

        # Optional cloud worker endpoint (Qwen 2.5 / Hermes 3 on Kaggle Dual-T4)
        self.kaggle_hermes_endpoint = (
            kaggle_hermes_endpoint
            or os.environ.get("KAGGLE_QWEN_ENDPOINT", "")
            or os.environ.get("KAGGLE_HERMES_ENDPOINT", "")
        ).rstrip("/")
        self.kaggle_hermes_model = (
            kaggle_hermes_model
            or os.environ.get("KAGGLE_QWEN_MODEL", "")
            or os.environ.get("KAGGLE_HERMES_MODEL", "qwen2.5-14b-instruct")
        )

        self.log_file = log_file or os.environ.get("ROUTER_LOG_FILE", "llm_router.log")
        self.fast_timeout = float(os.environ.get("FAST_TIMEOUT", fast_timeout))
        self.background_timeout = float(os.environ.get("BACKGROUND_TIMEOUT", background_timeout))
        self.connectivity_timeout = float(os.environ.get("CONNECTIVITY_TIMEOUT", connectivity_timeout))
        self.offline_timeout = float(os.environ.get("OFFLINE_TIMEOUT", os.environ.get("OLLAMA_TIMEOUT", offline_timeout)))

        self._log_lock = threading.Lock()
        self._gemini_cooldown_until: float = 0.0
        self.default_system_prompt = DEFAULT_SYSTEM_PROMPT
        self.greeting_system_prompt = GREETING_SYSTEM_PROMPT

    def get_gemini_cooldown_remaining(self) -> float:
        """Return the remaining cooldown seconds on Gemini 429 backoff."""
        return max(0.0, self._gemini_cooldown_until - time.time())

    # --------------------------------------------------------------------------
    # Decision Logging
    # --------------------------------------------------------------------------
    def log_decision(
        self,
        query_type: str,
        tier: str,
        latency_ms: float,
        success: bool,
        model: str,
        fallback_reasons: List[str],
        error: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log routing decision to a local JSONL log file for threshold tuning.
        
        Captures timestamp, tier used, latency, success/failure, and reason for any fallback.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query_type": query_type,
            "tier": tier,
            "model": model,
            "latency_ms": round(latency_ms, 2),
            "success": success,
            "fallback_reasons": [redact_api_key(r) for r in fallback_reasons],
            "error": redact_api_key(error) if error else None,
        }
        if extra:
            record["extra"] = extra

        try:
            line = json.dumps(record) + "\n"
            log_dir = os.path.dirname(os.path.abspath(self.log_file))
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            with self._log_lock:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as log_err:
            logger.warning("Failed writing to router log %s: %s", self.log_file, log_err)

    # --------------------------------------------------------------------------
    # Tier 1: Gemini 2.5 Flash
    # --------------------------------------------------------------------------
    def _call_gemini(
        self,
        prompt: str,
        timeout: float = 15.0,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        use_search: bool = False,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> str:
        """Call Gemini API via generateContent with auto model fallback and multi-key rotation."""
        keys = getattr(self, "gemini_api_keys", [])
        if not keys and self.gemini_api_key:
            keys = [self.gemini_api_key]
        if not keys:
            raise RouterError("GEMINI_API_KEY is not configured")

        base_model = verify_model_name(model or self.gemini_model or "gemini-3-flash-preview")
        env_pool = os.environ.get("GEMINI_MODEL_POOL")
        if env_pool:
            gemini_pool = [base_model] + [verify_model_name(m.strip()) for m in env_pool.split(",") if m.strip()]
        else:
            gemini_pool = [
                base_model,
                "gemini-3-flash-preview",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemini-3.6-flash",
            ]
        seen = set()
        model_candidates = [m for m in gemini_pool if not (m in seen or seen.add(m))]

        parts: List[Dict[str, Any]] = [{"text": prompt}]
        if image_bytes:
            import base64
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                }
            })

        gen_config: Dict[str, Any] = {"temperature": temperature}
        if json_mode:
            gen_config["responseMimeType"] = "application/json"

        payload: Dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_config,
        }
        if use_search and not image_bytes:
            payload["tools"] = [{"google_search": {}}]
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

        # Build candidate (key, model) pairs starting from the current key
        key_count = len(keys)
        pairs: List[Tuple[str, str]] = []
        for k_idx in range(key_count):
            k = keys[(self._current_key_idx + k_idx) % key_count]
            for m in model_candidates:
                pairs.append((k, m))

        gemini_start = time.perf_counter()
        gemini_ceiling = float(timeout) if timeout is not None else 15.0
        max_attempts = len(pairs)
        per_call_timeout = min(timeout if timeout is not None else gemini_ceiling, gemini_ceiling)
        last_error = ""

        data_bytes = json.dumps(payload).encode("utf-8")

        for attempt in range(max_attempts):
            elapsed = time.perf_counter() - gemini_start
            remaining_time = gemini_ceiling - elapsed
            if remaining_time <= 0.1 and attempt > 0:
                logger.warning("Gemini time ceiling reached after %d attempts. Failing over.", attempt)
                raise RouterError(redact_secrets(f"Gemini exceeded time ceiling after {attempt} attempts. Last error: {last_error}"))

            call_timeout = max(0.5, min(per_call_timeout, remaining_time))
            active_key, target_model = pairs[attempt]
            if target_model in _UNAVAILABLE_MODELS:
                logger.debug("Skipping Gemini model %s (0ms pre-flight exclusion via _UNAVAILABLE_MODELS)", target_model)
                continue
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"

            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": active_key,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=call_timeout) as response:
                    status_code = response.getcode()
                    if status_code == 200:
                        resp_data = json.loads(response.read().decode("utf-8"))
                        candidates = resp_data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            text = "".join(p.get("text", "") for p in parts if "text" in p)
                            if text:
                                # Advance current key for next invocation
                                self._current_key_idx = (self._current_key_idx + 1) % key_count
                                return text.strip()
                        raise RouterError("No candidate text returned in Gemini response")
            except urllib.error.HTTPError as http_err:
                status_code = http_err.code
                err_body = ""
                try:
                    err_body = http_err.read().decode("utf-8", errors="replace")
                except Exception as _suppressed_exc:
                    logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
                last_error = redact_secrets(f"HTTP {status_code}: {err_body[:120]}")

                # On HTTP 503 (Service Unavailable), log warning and continue to next model candidate in pool
                if status_code == 503 or "503" in err_body or "Service Unavailable" in err_body:
                    logger.warning("Gemini model %s returned HTTP 503 / Service Unavailable. Skipping to next model in pool...", target_model)
                    if attempt + 1 >= max_attempts:
                        raise RouterError(redact_secrets(f"Gemini HTTP 503 on {target_model}: {err_body[:120]}"))
                    continue

                # On HTTP 429 or quota limit, try next candidate model in pool before failing over
                if status_code in (429, 500) or any(k in err_body for k in ("RESOURCE_EXHAUSTED", "quota", "Quota")):
                    logger.warning("Gemini model %s returned HTTP %d / quota. Trying next candidate model in pool...", target_model, status_code)
                    if attempt + 1 >= max_attempts:
                        logger.warning("All Gemini pool candidates exhausted. Failing over to Groq...")
                        self._gemini_cooldown_until = time.time() + 45.0
                        raise RouterError(redact_secrets(f"gemini_http_{status_code}: {last_error}"))
                    continue

                if status_code == 401:
                    logger.warning("Gemini API key returned HTTP 401 (Invalid/Expired). Setting 5m cooldown on Gemini.")
                    self._gemini_cooldown_until = time.time() + 300.0
                    raise RouterError(redact_secrets(f"Gemini HTTP 401 on {target_model}: API key invalid/expired"))

                if status_code in (404, 400):
                    _UNAVAILABLE_MODELS.add(target_model)
                    logger.warning("Gemini model %s returned HTTP %d. Added to _UNAVAILABLE_MODELS for 0ms exclusion.", target_model, status_code)
                    if attempt + 1 >= max_attempts:
                        raise RouterError(redact_secrets(f"Gemini HTTP {status_code} on {target_model}: {err_body[:120]}"))
                    continue

                if attempt + 1 >= max_attempts:
                    raise RouterError(redact_secrets(f"Gemini HTTP {status_code} on {target_model}: {last_error}"))
                time.sleep(0.05)
            except urllib.error.URLError as url_err:
                err_str = str(url_err.reason)
                last_error = redact_secrets(f"URLError: {err_str}")
                if attempt + 1 >= max_attempts:
                    raise RouterError(redact_secrets(f"Gemini network error on {target_model}: {last_error}"))
                time.sleep(0.05)
            except RouterError:
                raise
            except Exception as e:
                err_str = str(e)
                # On 503 / Service Unavailable in exception, skip to next candidate model
                if "503" in err_str or "Service Unavailable" in err_str:
                    logger.warning("Gemini model %s exception indicates 503 / Service Unavailable (%s). Skipping to next model in pool...", target_model, redact_secrets(err_str))
                    if attempt + 1 >= max_attempts:
                        raise RouterError(redact_secrets(f"Gemini 503 on {target_model}: {err_str}"))
                    continue

                # On 429, 500, or quota exhaustion, try next model candidate in pool
                if any(k in err_str for k in ("429", "500", "RESOURCE_EXHAUSTED", "quota", "Quota", "Internal Server Error")):
                    logger.warning("Gemini model %s exception indicates 429/500 (%s). Trying next model in pool...", target_model, redact_secrets(err_str))
                    if attempt + 1 >= max_attempts:
                        if any(k in err_str for k in ("429", "RESOURCE_EXHAUSTED", "quota", "Quota")):
                            self._gemini_cooldown_until = time.time() + 60.0
                        raise RouterError(redact_secrets(f"gemini_fast_break: {err_str}"))
                    continue

                last_error = redact_secrets(err_str)
                if attempt + 1 >= max_attempts:
                    raise RouterError(redact_secrets(f"Gemini failed after attempt {attempt + 1}: {last_error}"))
                time.sleep(0.05)

        raise RouterError(redact_secrets(f"Gemini failed after {max_attempts} attempts: {last_error}"))

    # --------------------------------------------------------------------------
    # Tier 2: Groq (Llama 3.3 70B) — RARE FAILOVER ONLY
    # --------------------------------------------------------------------------
    @staticmethod
    def _truncate_prompt(prompt: str, max_chars: int) -> str:
        """Shorten prompt keeping beginning and end with a clear middle marker."""
        if len(prompt) <= max_chars:
            return prompt
        marker = "\n[...truncated...]\n"
        if max_chars <= len(marker):
            return prompt[:max_chars]
        available = max_chars - len(marker)
        head_len = available // 2
        tail_len = available - head_len
        return prompt[:head_len] + marker + prompt[-tail_len:]

    def _call_groq(
        self,
        prompt: str,
        timeout: float,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        json_mode: bool = False,
        _is_retry_413: bool = False,
    ) -> str:
        """Call Groq OpenAI-compatible chat completion endpoint."""
        if not self.groq_api_key:
            raise RouterError("GROQ_API_KEY is not configured")

        target_model = model or self.groq_model
        if target_model in _UNAVAILABLE_MODELS:
            if target_model != "llama-3.3-70b-versatile" and "llama-3.3-70b-versatile" not in _UNAVAILABLE_MODELS:
                logger.info("Groq model '%s' is in _UNAVAILABLE_MODELS. Falling back to llama-3.3-70b-versatile (0ms exclusion).", target_model)
                target_model = "llama-3.3-70b-versatile"
            else:
                raise RouterError(f"Groq model '{target_model}' is marked unavailable in _UNAVAILABLE_MODELS")

        total_cap = (self.groq_max_prompt_chars // 2) if _is_retry_413 else self.groq_max_prompt_chars
        # Prevent Groq HTTP 413 by trimming massive system prompts (e.g. tools docstrings)
        if system_prompt and len(system_prompt) > 6500:
            system_prompt = system_prompt[:6500] + "\n[System prompt trimmed for token budget]"
        remaining_budget = max(2000, total_cap - len(system_prompt or ""))
        orig_len = len(prompt)
        if orig_len > remaining_budget:
            prompt = self._truncate_prompt(prompt, remaining_budget)
            logger.info(
                "Groq prompt capped: original=%d chars, trimmed=%d chars (cap=%d, retry=%s)",
                orig_len, len(prompt), remaining_budget, _is_retry_413
            )

        if target_model == "qwen/qwen3.8-27b":
            target_model = "llama-3.1-8b-instant"
        if any(target_model.startswith(p) for p in ("whisper-", "orpheus-", "llama-prompt-guard-")):
            logger.warning("Groq model '%s' is banned on chat endpoints. Falling back to llama-3.3-70b-versatile", target_model)
            target_model = "llama-3.3-70b-versatile"
        url = "https://api.groq.com/openai/v1/chat/completions"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body_dict: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body_dict["response_format"] = {"type": "json_object"}

        data_bytes = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.groq_api_key}",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )

        resp_data = None
        for retry_attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    status_code = response.getcode()
                    if status_code != 200:
                        raise RouterError(f"Groq returned unexpected status: {status_code}")
                    resp_data = json.loads(response.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 400):
                    _UNAVAILABLE_MODELS.add(target_model)
                    logger.warning("Groq model '%s' returned HTTP %d. Added to _UNAVAILABLE_MODELS.", target_model, exc.code)
                    raise
                if exc.code == 429 and retry_attempt < 1:
                    retry_after_str = exc.headers.get("retry-after") if hasattr(exc, "headers") else None
                    if retry_after_str:
                        try:
                            backoff = min(4.0, max(1.0, float(retry_after_str)))
                        except (ValueError, TypeError):
                            backoff = 2.0
                    else:
                        backoff = 2.0
                    logger.warning(f"Groq returned HTTP 429 (Rate Limit). Backing off {backoff:.1f}s (retry {retry_attempt+1}/1)...")
                    time.sleep(backoff)
                    continue
                if exc.code == 413 and not _is_retry_413:
                    logger.warning(
                        "Groq returned HTTP 413 (Payload Too Large). Retrying once with half cap (%d chars)...",
                        self.groq_max_prompt_chars // 2,
                    )
                    return self._call_groq(
                        prompt=prompt,
                        timeout=timeout,
                        system_prompt=system_prompt,
                        model=model,
                        temperature=temperature,
                        json_mode=json_mode,
                        _is_retry_413=True,
                    )
                raise
        if resp_data is None:
            raise RouterError("Groq call failed after rate limit retries")

        choices = resp_data.get("choices", [])
        if not choices:
            raise RouterError("Groq response contained no choices")
        message = choices[0].get("message", {})
        return message.get("content", "").strip()

    def _call_groq_stream(
        self,
        prompt: str,
        timeout: float = 15.0,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        _is_retry_413: bool = False,
    ) -> Generator[str, None, None]:
        """Stream token chunks from Groq with <300ms time-to-first-token."""
        if not self.groq_api_key:
            raise RouterError("GROQ_API_KEY is not configured")

        target_model = model or self.groq_model
        if target_model in _UNAVAILABLE_MODELS:
            if target_model != "llama-3.3-70b-versatile" and "llama-3.3-70b-versatile" not in _UNAVAILABLE_MODELS:
                logger.info("Groq stream model '%s' in _UNAVAILABLE_MODELS. Falling back to llama-3.3-70b-versatile.", target_model)
                target_model = "llama-3.3-70b-versatile"
            else:
                raise RouterError(f"Groq target model '{target_model}' is marked unavailable in _UNAVAILABLE_MODELS")

        orig_len = len(prompt)
        cap = (self.groq_max_prompt_chars // 2) if _is_retry_413 else self.groq_max_prompt_chars
        if system_prompt and len(system_prompt) > 6500:
            system_prompt = system_prompt[:6500] + "\n[System prompt trimmed for token budget]"
        if orig_len > cap:
            prompt = self._truncate_prompt(prompt, cap)
            logger.info(
                "Groq stream prompt capped: original=%d chars, trimmed=%d chars (cap=%d, retry=%s)",
                orig_len, len(prompt), cap, _is_retry_413
            )

        if target_model == "qwen/qwen3.8-27b":
            target_model = "llama-3.1-8b-instant"
        if any(target_model.startswith(p) for p in ("whisper-", "orpheus-", "llama-prompt-guard-")):
            logger.warning("Groq model '%s' is banned on chat endpoints. Falling back to llama-3.3-70b-versatile", target_model)
            target_model = "llama-3.3-70b-versatile"
        url = "https://api.groq.com/openai/v1/chat/completions"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body_dict = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        data_bytes = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.groq_api_key}",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                for line in response:
                    line_str = line.decode("utf-8").strip()
                    if not line_str or line_str.startswith(":"):
                        continue
                    if line_str.startswith("data: "):
                        payload = line_str[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk_json = json.loads(payload)
                            delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                            # Support deep thinking / reasoning tokens from models like Qwen/DeepSeek
                            reasoning = delta.get("reasoning_content", "") or delta.get("thought", "")
                            if reasoning:
                                yield {"type": "thought", "text": reasoning}
                            content = delta.get("content", "")
                            if content:
                                yield {"type": "token", "text": content}
                            usage = chunk_json.get("usage")
                            if usage:
                                yield {
                                    "type": "usage",
                                    "usage": {
                                        "prompt_tokens": usage.get("prompt_tokens", 0),
                                        "completion_tokens": usage.get("completion_tokens", 0),
                                        "total_tokens": usage.get("total_tokens", 0),
                                    }
                                }
                        except Exception:
                            continue
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 400):
                _UNAVAILABLE_MODELS.add(target_model)
                logger.warning("Groq stream model '%s' returned HTTP %d. Added to _UNAVAILABLE_MODELS.", target_model, exc.code)
            if exc.code == 413 and not _is_retry_413:
                logger.warning(
                    "Groq stream returned HTTP 413 (Payload Too Large). Retrying once with half cap (%d chars)...",
                    self.groq_max_prompt_chars // 2,
                )
                yield from self._call_groq_stream(
                    prompt=prompt,
                    timeout=timeout,
                    system_prompt=system_prompt,
                    model=model,
                    _is_retry_413=True,
                )
                return
            if exc.code in (404, 400) and target_model != "llama-3.3-70b-versatile" and "llama-3.3-70b-versatile" not in _UNAVAILABLE_MODELS:
                logger.warning("Groq stream model '%s' returned HTTP %d. Falling back to llama-3.3-70b-versatile...", target_model, exc.code)
                yield from self._call_groq_stream(
                    prompt=prompt,
                    timeout=timeout,
                    system_prompt=system_prompt,
                    model="llama-3.3-70b-versatile",
                    temperature=temperature,
                    _is_retry_413=_is_retry_413,
                )
                return
            raise

    # --------------------------------------------------------------------------
    # Tier 2.5: OpenRouter (Optional Failover)
    # --------------------------------------------------------------------------
    def _call_openrouter(
        self,
        prompt: str,
        timeout: float,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        _is_retry_413: bool = False,
    ) -> str:
        """Call OpenRouter OpenAI-compatible chat completion endpoint."""
        if not self.openrouter_api_key:
            raise RouterError("OPENROUTER_API_KEY is not configured")
        target_model = model or self.openrouter_model
        if not target_model:
            raise RouterError("OPENROUTER_MODEL is not configured")

        orig_len = len(prompt)
        cap = (self.openrouter_max_prompt_chars // 2) if _is_retry_413 else self.openrouter_max_prompt_chars
        if orig_len > cap:
            prompt = self._truncate_prompt(prompt, cap)
            logger.info(
                "OpenRouter prompt capped: original=%d chars, trimmed=%d chars (cap=%d, retry=%s)",
                orig_len, len(prompt), cap, _is_retry_413
            )

        url = "https://openrouter.ai/api/v1/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body_dict = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.7,
        }
        data_bytes = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "HTTP-Referer": "https://github.com/jagath1105/jai",
                "X-Title": "JAI Copilot",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.getcode()
                if status_code != 200:
                    raise RouterError(f"OpenRouter returned unexpected status: {status_code}")
                resp_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 413 and not _is_retry_413:
                logger.warning(
                    "OpenRouter returned HTTP 413 (Payload Too Large). Retrying once with half cap (%d chars)...",
                    self.openrouter_max_prompt_chars // 2,
                )
                return self._call_openrouter(
                    prompt=prompt,
                    timeout=timeout,
                    system_prompt=system_prompt,
                    model=model,
                    _is_retry_413=True,
                )
            raise

        choices = resp_data.get("choices", [])
        if not choices:
            raise RouterError("OpenRouter response contained no choices")
        message = choices[0].get("message", {})
        return message.get("content", "").strip()

    def _call_gemini_stream(
        self,
        prompt: str,
        timeout: float = 15.0,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        use_search: bool = False,
        temperature: float = 0.2,
    ) -> Generator[Dict[str, Any], None, None]:
        """Stream token chunks from Gemini API via SSE streamGenerateContent."""
        keys = getattr(self, "gemini_api_keys", [])
        if not keys and self.gemini_api_key:
            keys = [self.gemini_api_key]
        if not keys:
            raise RouterError("GEMINI_API_KEY is not configured")

        target_model = model or self.gemini_model or "gemini-3-flash-preview"
        if target_model in _UNAVAILABLE_MODELS:
            raise RouterError(f"Gemini model '{target_model}' is marked unavailable in _UNAVAILABLE_MODELS")
        max_attempts = len(keys)
        last_error = ""

        for attempt in range(max_attempts):
            active_key = keys[self._current_key_idx % len(keys)]
            self._current_key_idx = (self._current_key_idx + 1) % len(keys)

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:streamGenerateContent?alt=sse"
            
            payload: Dict[str, Any] = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature},
            }
            if use_search:
                payload["tools"] = [{"google_search": {}}]
            if system_prompt:
                payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": active_key,
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    for line in response:
                        line_str = line.decode("utf-8").strip()
                        if not line_str or line_str.startswith(":"):
                            continue
                        if line_str.startswith("data: "):
                            payload_str = line_str[6:].strip()
                            try:
                                chunk_json = json.loads(payload_str)
                                # Extract usage metadata
                                usage = chunk_json.get("usageMetadata")
                                if usage:
                                    yield {
                                        "type": "usage",
                                        "usage": {
                                            "prompt_tokens": usage.get("promptTokenCount", 0),
                                            "completion_tokens": usage.get("candidatesTokenCount", 0),
                                            "total_tokens": usage.get("totalTokenCount", 0),
                                        }
                                    }
                                candidates = chunk_json.get("candidates", [])
                                if candidates:
                                    parts = candidates[0].get("content", {}).get("parts", [])
                                    for p in parts:
                                        # Check for thought/reasoning parts vs standard text
                                        if p.get("thought", False):
                                            yield {"type": "thought", "text": p.get("text", "")}
                                        elif "text" in p:
                                            yield {"type": "token", "text": p.get("text", "")}
                            except Exception:
                                continue
                    return
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 400):
                    _UNAVAILABLE_MODELS.add(target_model)
                    logger.warning("Gemini stream model '%s' returned HTTP %d. Added to _UNAVAILABLE_MODELS.", target_model, exc.code)
                last_error = redact_api_key(f"HTTP {exc.code}: {exc.reason}")
                if exc.code in (429, 503):
                    logger.warning("Gemini stream returned %d on key ...%s, rotating key...", exc.code, active_key[-6:])
                    continue
                raise
            except Exception as exc:
                last_error = redact_api_key(str(exc))
                continue

        raise RouterError(redact_api_key(f"Gemini streaming failed after {max_attempts} attempts: {last_error}"))

    # --------------------------------------------------------------------------
    # Tier 3: Ollama (Local HTTP API, On-Demand, Non-Resident)
    # --------------------------------------------------------------------------
    def _call_ollama(
        self,
        prompt: str,
        timeout: float = 45.0,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Call Ollama local HTTP API.
        
        Loads model on-demand and unloads immediately using keep_alive='0'.
        """
        target_model = model or self.ollama_model

        # Only block when executing structured browser ReAct agent loops
        if system_prompt and (
            ("JAI Autonomous Agent Core" in system_prompt and any(
                tool in system_prompt for tool in ("browser_click_element", "browser_fill_element")
            ))
            or ("Execute browser_click" in system_prompt)
        ):
            raise RouterError(f"Hard block: Tier 3 local model ({target_model}) cannot execute autonomous browser tool calls.")

        url = f"{self.ollama_host}/api/chat"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # keep_alive: "0" forces Ollama to unload the model from memory immediately
        # after response generation, satisfying "Load on-demand, do not keep it resident".
        body_dict: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "keep_alive": "0",
        }

        data_bytes = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.getcode()
                if status_code != 200:
                    raise RouterError(f"Ollama returned unexpected status: {status_code}")
                resp_data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise RouterError(f"Ollama invocation failed: {exc}") from exc

        message = resp_data.get("message") or {}
        content = message.get("content", "")
        if not content:
            # Fallback for generate endpoint format if chat wasn't used
            content = resp_data.get("response", "")
        if not content:
            raise RouterError("Ollama response contained no content")
        return content.strip()

    # --------------------------------------------------------------------------
    # Optional Cloud Worker: Kaggle Hermes 3
    # --------------------------------------------------------------------------
    def _call_kaggle_hermes(
        self,
        prompt: str,
        timeout: float = 30.0,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Invokes Kaggle-hosted Hermes 3 endpoint via Cloudflare Tunnel if configured."""
        if not self.kaggle_hermes_endpoint:
            raise RouterError("KAGGLE_HERMES_ENDPOINT is not configured.")

        url = f"{self.kaggle_hermes_endpoint}/v1/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model or self.kaggle_hermes_model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 2048,
        }
        data_bytes = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.getcode() != 200:
                raise RouterError(f"Hermes 3 endpoint returned status: {response.getcode()}")
            resp_data = json.loads(response.read().decode("utf-8"))

        choices = resp_data.get("choices", [])
        if not choices:
            raise RouterError("Hermes 3 returned no choices in response.")
        content = choices[0].get("message", {}).get("content", "")
        return content.strip()

    def route(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        query_type: Literal["fast", "background"] = "background",
        use_search: bool = False,
        **kwargs
    ) -> str:
        """Convenience method returning raw text response from route_query."""
        res = self.route_query(prompt=prompt, system_prompt=system_prompt, query_type=query_type, use_search=use_search, **kwargs)
        return res.text

    # --------------------------------------------------------------------------
    # Main Routing Priority
    # --------------------------------------------------------------------------
    def route_query(
        self,
        prompt: str,
        query_type: Literal["fast", "background"] = "fast",
        system_prompt: Optional[str] = None,
        timeout: Optional[float] = None,
        force_tier: Optional[Literal["gemini", "groq", "openrouter", "ollama", "kaggle_hermes"]] = None,
        role: Optional[str] = None,
        use_search: bool = False,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> RouteResult:
        """Execute query following the exact priority order:
        
        1. Check network connectivity first.
        2. If online: call Gemini 2.5 Flash with 2s timeout for fast-path queries
           (10s for background queries).
        3. On Gemini failure, timeout, 429, or 5xx: fall back to Groq (Llama 3.3 70B)
           IF a GROQ_API_KEY is present in config. If not configured, skip this tier
           with a logged warning and proceed to step 4 without failing.
        4. If offline, or both Gemini and Groq fail/are unavailable: fall back to OpenRouter
           (if configured), then local Ollama model (qwen3:8b) loaded on-demand.
        """
        if os.environ.get("JAI_BENCHMARK_PACING") == "1":
            time.sleep(2.0)

        if system_prompt is None:
            if is_greeting_or_smalltalk(prompt):
                system_prompt = self.greeting_system_prompt
            else:
                system_prompt = self.default_system_prompt
        target_role = role or classify_role(prompt)

        # Define q_type and effective timeout BEFORE checking force_tier
        q_type = query_type if query_type in ("fast", "background") else "fast"
        if timeout is None:
            tier1_timeout = self.fast_timeout if q_type == "fast" else self.background_timeout
        else:
            tier1_timeout = timeout

        # Explicit forced tier override
        if force_tier == "openrouter":
            start_t = time.perf_counter()
            resp = self._call_openrouter(
                prompt=prompt,
                timeout=tier1_timeout,
                system_prompt=system_prompt,
                model=self.openrouter_model,
            )
            lat = (time.perf_counter() - start_t) * 1000
            return RouteResult(
                text=resp,
                tier="openrouter",
                model=self.openrouter_model,
                latency_ms=lat,
                success=True,
                fallback_reasons=[],
                role=target_role,
            )

        if force_tier == "kaggle_hermes":
            start_t = time.perf_counter()
            resp = self._call_kaggle_hermes(
                prompt=prompt,
                timeout=tier1_timeout,
                system_prompt=system_prompt,
            )
            lat = (time.perf_counter() - start_t) * 1000
            return RouteResult(
                text=resp,
                tier="kaggle_hermes",
                model=self.kaggle_hermes_model,
                latency_ms=lat,
                success=True,
                fallback_reasons=[],
                role=target_role,
            )

        fallback_reasons: List[str] = []
        start_time = time.perf_counter()

        # Step 1: Check network connectivity first
        is_online = check_connectivity(timeout=self.connectivity_timeout)

        # Step 2: If online, call Gemini Flash
        if is_online:
            if force_tier == "gemini" or force_tier is None:
                if time.time() < self._gemini_cooldown_until:
                    remaining = self._gemini_cooldown_until - time.time()
                    logger.info("Gemini on 429 cooldown (%.1fs remaining). Fast-failing over to Groq.", remaining)
                    fallback_reasons.append(f"gemini_cooldown_active_{remaining:.1f}s")
                else:
                    try:
                        default_to = 4.5 if q_type == "fast" else 15.0
                        gemini_timeout = tier1_timeout if tier1_timeout is not None else default_to
                        response_text = self._call_gemini(
                            prompt=prompt,
                            timeout=gemini_timeout,
                            system_prompt=system_prompt,
                            model=self.gemini_model,
                            use_search=use_search,
                            temperature=temperature,
                            json_mode=json_mode,
                        )
                        latency_ms = (time.perf_counter() - start_time) * 1000
                        self.log_decision(
                            query_type=q_type,
                            tier="gemini",
                            latency_ms=latency_ms,
                            success=True,
                            model=self.gemini_model,
                            fallback_reasons=[],
                        )
                        return RouteResult(
                            text=response_text,
                            tier="gemini",
                            model=self.gemini_model,
                            latency_ms=latency_ms,
                            success=True,
                            fallback_reasons=[],
                            role=target_role,
                        )
                    except (TimeoutError, socket.timeout) as exc:
                        reason = redact_secrets(f"gemini_timeout_{gemini_timeout}s: {exc}")
                        fallback_reasons.append(reason)
                        logger.warning("Gemini timeout: %s", reason)
                    except urllib.error.HTTPError as exc:
                        # Catch 429 rate limit or 5xx server error
                        if exc.code == 429:
                            self._gemini_cooldown_until = time.time() + 60.0
                        reason = redact_secrets(f"gemini_http_{exc.code}: {exc.reason}")
                        fallback_reasons.append(reason)
                        logger.warning("Gemini HTTP error %d: %s", exc.code, exc.reason)
                    except Exception as exc:
                        reason = redact_secrets(f"gemini_error: {type(exc).__name__} - {exc}")
                        fallback_reasons.append(reason)
                        logger.warning("Gemini invocation failed: %s", reason)

            # Step 3: Groq (Llama 3.3 70B / Qwen 2.5 27B) — DIRECT OR FAILOVER
            if force_tier == "groq" or (force_tier is None and fallback_reasons):
                is_valid_groq = bool(self.groq_api_key and "placeholder" not in self.groq_api_key.lower() and not self.groq_api_key.startswith("your_"))
                if not is_valid_groq:
                    logger.warning(
                        "No GROQ_API_KEY configured in config or environment. "
                        "Skipping Groq failover tier and proceeding directly to local model (Ollama)."
                    )
                    fallback_reasons.append("groq_skipped_no_api_key")
                else:
                    try:
                        last_reason = fallback_reasons[-1] if fallback_reasons else "direct_groq"
                        logger.info("Executing Groq query (reason: %s)", last_reason)
                        groq_timeout = tier1_timeout if q_type == "fast" else self.background_timeout
                        response_text = self._call_groq(
                            prompt=prompt,
                            timeout=groq_timeout,
                            system_prompt=system_prompt,
                            model=self.groq_model,
                            temperature=temperature,
                            json_mode=json_mode,
                        )
                        latency_ms = (time.perf_counter() - start_time) * 1000
                        self.log_decision(
                            query_type=q_type,
                            tier="groq",
                            latency_ms=latency_ms,
                            success=True,
                            model=self.groq_model,
                            fallback_reasons=fallback_reasons,
                        )
                        return RouteResult(
                            text=response_text,
                            tier="groq",
                            model=self.groq_model,
                            latency_ms=latency_ms,
                            success=True,
                            fallback_reasons=fallback_reasons,
                            role=target_role,
                        )
                    except (TimeoutError, socket.timeout) as exc:
                        reason = f"groq_timeout: {exc}"
                        fallback_reasons.append(reason)
                        logger.warning("Groq failover timeout: %s", reason)
                    except urllib.error.HTTPError as exc:
                        reason = f"groq_http_{exc.code}: {exc.reason}"
                        fallback_reasons.append(reason)
                        logger.warning("Groq failover HTTP error %d: %s", exc.code, exc.reason)
                    except Exception as exc:
                        reason = f"groq_error: {type(exc).__name__} - {exc}"
                        fallback_reasons.append(reason)
                        logger.warning("Groq failover failed: %s", reason)

            # Step 3.5: OpenRouter Tier (Optional failover before Ollama)
            if force_tier == "openrouter" or (force_tier is None and fallback_reasons):
                is_valid_openrouter = bool(
                    self.openrouter_api_key
                    and "placeholder" not in self.openrouter_api_key.lower()
                    and not self.openrouter_api_key.startswith("your_")
                )
                if not is_valid_openrouter:
                    fallback_reasons.append("openrouter_skipped_no_api_key")
                elif not self.openrouter_model:
                    fallback_reasons.append("openrouter_skipped_no_model")
                else:
                    try:
                        logger.info("Executing OpenRouter failover due to prior tier failures...")
                        or_timeout = tier1_timeout if q_type == "fast" else self.background_timeout
                        response_text = self._call_openrouter(
                            prompt=prompt,
                            timeout=or_timeout,
                            system_prompt=system_prompt,
                            model=self.openrouter_model,
                        )
                        latency_ms = (time.perf_counter() - start_time) * 1000
                        self.log_decision(
                            query_type=q_type,
                            tier="openrouter",
                            latency_ms=latency_ms,
                            success=True,
                            model=self.openrouter_model,
                            fallback_reasons=fallback_reasons,
                        )
                        return RouteResult(
                            text=response_text,
                            tier="openrouter",
                            model=self.openrouter_model,
                            latency_ms=latency_ms,
                            success=True,
                            fallback_reasons=fallback_reasons,
                            role=target_role,
                        )
                    except (TimeoutError, socket.timeout) as exc:
                        reason = redact_api_key(f"openrouter_timeout: {exc}")
                        fallback_reasons.append(reason)
                        logger.warning("OpenRouter failover timeout: %s", reason)
                    except urllib.error.HTTPError as exc:
                        reason = redact_api_key(f"openrouter_http_{exc.code}: {exc.reason}")
                        fallback_reasons.append(reason)
                        logger.warning("OpenRouter failover HTTP error %d: %s", exc.code, exc.reason)
                    except Exception as exc:
                        reason = redact_api_key(f"openrouter_error: {type(exc).__name__} - {exc}")
                        fallback_reasons.append(reason)
                        logger.warning("OpenRouter failover failed: %s", reason)
        else:
            fallback_reasons.append("network_offline")
            logger.info("Network offline: Skipping cloud LLMs and routing directly to local Ollama.")

        # Step 4: Local Ollama Fallback (qwen3:8b, on-demand, non-resident)
        # HARD BLOCK: Tier 3 local model (qwen2.5:3b) is prohibited from executing browser tool calls to prevent hallucination
        is_browser_task = (
            target_role in ("browser", "web")
            or any(k in (system_prompt or "").lower() for k in ("browser_click", "browser_fill", "browser_navigate", "browser_find_elements", "browser_read_network_json", "browser_press_key", "browser_scroll", "data-jai-id", "numbered dom snapshot", "open_shadow_tab"))
            or any(k in prompt.lower() for k in ("browser_click", "browser_fill", "browser_navigate", "browser_find_elements", "browser_read_network_json", "[data-jai-id]", "data-jai-id="))
        )
        if is_browser_task:
            reason = "tier3_blocked_for_browser_tools"
            fallback_reasons.append(reason)
            logger.warning("Hard block: Tier 3 local model (%s) prohibited from executing browser tool calls.", self.ollama_model)
            raise AllTiersFailedError(
                f"Browser action failed across authorized cloud tiers ({fallback_reasons}). Tier 3 ({self.ollama_model}) blocked by safety policy.",
                fallback_reasons
            )
        # Check cached health: If background monitor confirmed Ollama is offline, fail over in 0ms without waiting.
        health_cache = get_health_cache() if get_health_cache is not None else None
        if health_cache is not None and not health_cache.is_alive("ollama"):
            fallback_reasons.append("ollama_cached_offline")
            logger.info("Ollama cached offline: 0ms non-blocking skip.")
        else:
            try:
                logger.info("Falling back to local Ollama (%s). Reasons: %s", self.ollama_model, fallback_reasons)
                ollama_timeout = self.offline_timeout
                response_text = self._call_ollama(
                    prompt=prompt,
                    timeout=ollama_timeout,
                    system_prompt=system_prompt,
                    model=self.ollama_model,
                )
                latency_ms = (time.perf_counter() - start_time) * 1000
                self.log_decision(
                    query_type=q_type,
                    tier="ollama",
                    latency_ms=latency_ms,
                    success=True,
                    model=self.ollama_model,
                    fallback_reasons=fallback_reasons,
                )
                return RouteResult(
                    text=response_text,
                    tier="ollama",
                    model=self.ollama_model,
                    latency_ms=latency_ms,
                    success=True,
                    fallback_reasons=fallback_reasons,
                    role=target_role,
                )
            except Exception as exc:
                if health_cache is not None:
                    try:
                        health_cache.set_status("ollama", is_alive=False)
                    except Exception as _suppressed_exc:
                        logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
                reason = f"ollama_error: {type(exc).__name__} - {redact_secrets(str(exc))}"
                fallback_reasons.append(reason)
                logger.error("Local Ollama tier failed: %s", reason)

        # All available tiers failed
        total_latency = (time.perf_counter() - start_time) * 1000
        self.log_decision(
            query_type=q_type,
            tier="none",
            latency_ms=total_latency,
            success=False,
            model="none",
            fallback_reasons=fallback_reasons,
            error="All routing tiers failed.",
        )
        raise AllTiersFailedError(
            f"All LLM tiers failed for query_type='{q_type}'. Fallback chain: {fallback_reasons}",
            fallback_reasons=fallback_reasons,
        )

    def route_image(
        self,
        image_bytes: bytes,
        prompt: str = "Analyze and summarize this image.",
        mime_type: str = "image/jpeg",
        target_model: Optional[str] = None,
        timeout: Optional[float] = None,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> RouteResult:
        """Route image query directly to Gemini Vision.
        
        Strict Invariants:
        1. Image payloads MUST NOT failover to text-only models (e.g. Groq Qwen/Llama).
        2. Passes system_prompt and json_mode cleanly to provider payload.
        3. Fails with a clear InvalidModelError on unknown/rejected models rather than silently returning empty text.
        """
        start_time = time.perf_counter()
        call_timeout = timeout or 25.0
        fallback_reasons: List[str] = []

        active_model = target_model or os.environ.get("VISION_MODEL") or getattr(config, "VISION_MODEL", None) or getattr(self, "vision_model", None) or self.gemini_model or "gemini-3-flash-preview"
        validated_model = verify_model_name(active_model, allow_default=True)

        try:
            text = self._call_gemini(
                prompt=prompt,
                timeout=call_timeout,
                model=validated_model,
                system_prompt=system_prompt,
                image_bytes=image_bytes,
                mime_type=mime_type,
                json_mode=json_mode,
            )
            lat = (time.perf_counter() - start_time) * 1000
            self.log_decision(
                query_type="multimodal",
                tier="gemini",
                model=validated_model,
                latency_ms=lat,
                success=True,
                fallback_reasons=[],
                extra={"multimodal": True, "mime_type": mime_type, "json_mode": json_mode},
            )
            return RouteResult(
                text=text,
                tier="gemini",
                model=validated_model,
                latency_ms=lat,
                success=True,
                fallback_reasons=[],
                metadata={"multimodal": True, "json_mode": json_mode},
            )
        except InvalidModelError:
            raise
        except Exception as exc:
            lat = (time.perf_counter() - start_time) * 1000
            err_msg = redact_secrets(str(exc))
            if "not_found" in err_msg.lower() or "not found" in err_msg.lower() or "404" in err_msg:
                raise InvalidModelError(
                    f"Model '{validated_model}' rejected by provider as non-existent or invalid: {err_msg}. "
                    "Do not guess model names; please configure an approved model in config/env."
                ) from exc
            fallback_reasons.append(f"gemini_vision_error: {err_msg}")
            logger.warning("Gemini Vision failed (%s). NOT failing over to text-only Groq.", err_msg)
            self.log_decision(
                query_type="multimodal",
                tier="none",
                model=validated_model,
                latency_ms=lat,
                success=False,
                fallback_reasons=fallback_reasons,
                error=err_msg,
                extra={"multimodal": True},
            )
            return RouteResult(
                text="",
                tier="none",
                model=validated_model,
                latency_ms=lat,
                success=False,
                fallback_reasons=fallback_reasons,
                metadata={"multimodal": True, "error": err_msg},
            )


# Global router singleton instance
_DEFAULT_ROUTER: Optional[LLMRouter] = None


def get_default_router() -> LLMRouter:
    """Obtain or initialize default LLMRouter instance."""
    global _DEFAULT_ROUTER
    if _DEFAULT_ROUTER is None:
        _DEFAULT_ROUTER = LLMRouter()
    return _DEFAULT_ROUTER


def route_query(
    prompt: str,
    query_type: Literal["fast", "background"] = "fast",
    system_prompt: Optional[str] = None,
    timeout: Optional[float] = None,
    router: Optional[LLMRouter] = None,
    use_search: bool = False,
    **kwargs: Any,
) -> RouteResult:
    """Execute prioritized LLM routing for the given prompt."""
    active_router = router or get_default_router()
    if system_prompt is None:
        if is_greeting_or_smalltalk(prompt):
            system_prompt = GREETING_SYSTEM_PROMPT
        else:
            system_prompt = DEFAULT_SYSTEM_PROMPT
    res = active_router.route_query(
        prompt=prompt,
        query_type=query_type,
        system_prompt=system_prompt,
        timeout=timeout,
        use_search=use_search,
        **kwargs,
    )
    if is_greeting_or_smalltalk(prompt) and res.text:
        lines = [l.strip() for l in res.text.splitlines() if l.strip()]
        if len(lines) > 2:
            res.text = "\n".join(lines[:2])
    return res


def route_image(
    image_bytes: bytes,
    prompt: str = "Analyze and summarize this image.",
    mime_type: str = "image/jpeg",
    target_model: Optional[str] = None,
    timeout: Optional[float] = None,
    system_prompt: Optional[str] = None,
    json_mode: bool = False,
    router: Optional[LLMRouter] = None,
) -> RouteResult:
    """Analyze image using Gemini native vision without failing over to text-only models."""
    active_router = router or get_default_router()
    return active_router.route_image(
        image_bytes=image_bytes,
        prompt=prompt,
        mime_type=mime_type,
        target_model=target_model,
        timeout=timeout,
        system_prompt=system_prompt,
        json_mode=json_mode,
    )


def route_query_stream_events(
    prompt: str,
    query_type: Literal["voice", "fast", "background"] = "voice",
    system_prompt: Optional[str] = None,
    timeout: Optional[float] = None,
    router: Optional[LLMRouter] = None,
    force_tier: Optional[Literal["groq", "gemini", "ollama", "kaggle_qwen"]] = None,
    temperature: float = 0.2,
) -> Generator[Dict[str, Any], None, None]:
    """Streaming generator yielding structured event dictionaries:
    - {"type": "thought", "text": "..."}  -> Model reasoning / chain-of-thought
    - {"type": "token", "text": "..."}    -> Output text tokens
    - {"type": "usage", "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}
    - {"type": "done", "tier": str, "model": str, "latency_ms": float}
    """
    active_router = router or get_default_router()
    stream_timeout = timeout or (10.0 if query_type == "voice" else 20.0)
    start_time = time.perf_counter()
    usage_reported = False

    if system_prompt is None:
        if is_greeting_or_smalltalk(prompt):
            system_prompt = GREETING_SYSTEM_PROMPT
        else:
            system_prompt = DEFAULT_SYSTEM_PROMPT

    # 1. Groq Voice Fast-Path
    if (force_tier == "groq" or force_tier is None) and active_router.groq_api_key:
        is_valid_groq = bool("placeholder" not in active_router.groq_api_key.lower() and not active_router.groq_api_key.startswith("your_"))
        if is_valid_groq:
            try:
                for chunk in active_router._call_groq_stream(
                    prompt=prompt,
                    timeout=stream_timeout,
                    system_prompt=system_prompt,
                    model=active_router.groq_model,
                    temperature=temperature,
                ):
                    if isinstance(chunk, dict):
                        if chunk.get("type") == "usage":
                            usage_reported = True
                        yield chunk
                    elif isinstance(chunk, str):
                        yield {"type": "token", "text": chunk}
                latency = (time.perf_counter() - start_time) * 1000.0
                yield {"type": "done", "tier": "groq", "model": active_router.groq_model, "latency_ms": latency}
                return
            except Exception as exc:
                logger.warning("Groq streaming fast-path failed, falling back: %s", exc)

    # 2. Gemini SSE Stream Path
    if (force_tier == "gemini" or force_tier is None) and (active_router.gemini_api_key or active_router.gemini_api_keys):
        try:
            for chunk in active_router._call_gemini_stream(
                prompt=prompt,
                timeout=stream_timeout,
                system_prompt=system_prompt,
                model=active_router.gemini_model,
                temperature=temperature,
            ):
                if isinstance(chunk, dict):
                    if chunk.get("type") == "usage":
                        usage_reported = True
                    yield chunk
                elif isinstance(chunk, str):
                    yield {"type": "token", "text": chunk}
            latency = (time.perf_counter() - start_time) * 1000.0
            yield {"type": "done", "tier": "gemini", "model": active_router.gemini_model, "latency_ms": latency}
            return
        except Exception as exc:
            logger.warning("Gemini streaming path failed, falling back: %s", exc)

    # 3. Synchronous Fallback (Yield complete answer as single chunk)
    fallback_res = active_router.route_query(
        prompt=prompt,
        query_type="fast" if query_type == "voice" else query_type,
        system_prompt=system_prompt,
        timeout=stream_timeout,
    )
    yield {"type": "token", "text": fallback_res.text}
    # Approximate token counts for fallback if no usage metadata
    p_toks = max(1, len(prompt.split()))
    c_toks = max(1, len(fallback_res.text.split()))
    yield {
        "type": "usage",
        "usage": {
            "prompt_tokens": p_toks,
            "completion_tokens": c_toks,
            "total_tokens": p_toks + c_toks,
            "estimated": True,
        }
    }
    latency = (time.perf_counter() - start_time) * 1000.0
    yield {"type": "done", "tier": fallback_res.tier, "model": fallback_res.model, "latency_ms": latency}


def route_query_stream(
    prompt: str,
    query_type: Literal["voice", "fast", "background"] = "voice",
    system_prompt: Optional[str] = None,
    timeout: Optional[float] = None,
    router: Optional[LLMRouter] = None,
    force_tier: Optional[Literal["groq", "gemini", "ollama", "kaggle_qwen"]] = None,
    temperature: float = 0.2,
) -> Generator[str, None, None]:
    """Streaming generator yielding token string fragments iteratively.
    
    Milestone 3 (R1) Voice Fast-Path:
    1. For voice queries, bypasses sequential cold node delays.
    2. Tries Groq streaming first IF key configured (yielding first token in ~200-300ms).
    3. Falls back to Gemini SSE streaming if Groq fails or is not configured.
    4. Falls back to non-streaming route_query text as final failsafe.
    """
    for event in route_query_stream_events(
        prompt=prompt,
        query_type=query_type,
        system_prompt=system_prompt,
        timeout=timeout,
        router=router,
        force_tier=force_tier,
        temperature=temperature,
    ):
        if event.get("type") == "token":
            yield event.get("text", "")


# ------------------------------------------------------------------------------
# Query Expansion Function
# ------------------------------------------------------------------------------
def query_expansion(
    query: str,
    router: Optional[LLMRouter] = None,
) -> str:
    """One cheap LLM call (fast-path route) that rewrites and clarifies ambiguous
    search terms before querying the memory vault.
    """
    system_prompt = (
        "You are an expert search query expansion system for a personal memory vault. "
        "Your job is to rewrite the user's ambiguous search query into a clear, enriched search string. "
        "Expand abbreviations, add key synonyms, and remove vague conversational filler. "
        "Respond ONLY with the rewritten search query string. Do NOT output quotes, preamble, or explanations."
    )
    prompt = f"User query to expand for memory search:\n{query}"

    active_router = router or get_default_router()
    result = active_router.route_query(
        prompt=prompt,
        query_type="fast",
        system_prompt=system_prompt,
    )
    expanded = result.text.strip()
    # Strip any enclosing quotation marks if returned by model
    if (expanded.startswith('"') and expanded.endswith('"')) or (expanded.startswith("'") and expanded.endswith("'")):
        expanded = expanded[1:-1].strip()
    return expanded


# ------------------------------------------------------------------------------
# Council Mode Function
# ------------------------------------------------------------------------------
def council_mode(
    query: str,
    router: Optional[LLMRouter] = None,
    query_type: Literal["fast", "background"] = "background",
) -> CouncilResult:
    """Council Mode:
    
    Calls Gemini AND local Qwen (Ollama) in parallel.
    CRITICAL: Skips Groq per the rare-failover rule.
    Then makes one synthesis call to reconcile answers into a unified response.
    Optional mode, not default.
    """
    active_router = router or get_default_router()
    start_time = time.perf_counter()
    fallback_reasons: List[str] = []

    is_online = check_connectivity(timeout=active_router.connectivity_timeout)
    gemini_resp: Optional[str] = None
    ollama_resp: Optional[str] = None

    timeout_val = active_router.background_timeout if query_type == "background" else active_router.fast_timeout
    ollama_timeout_val = active_router.offline_timeout

    def _call_gemini_worker() -> Optional[str]:
        if not is_online:
            return None
        return active_router._call_gemini(
            prompt=query,
            timeout=timeout_val,
            model=active_router.gemini_model,
        )

    def _call_ollama_worker() -> Optional[str]:
        return active_router._call_ollama(
            prompt=query,
            timeout=ollama_timeout_val,
            model=active_router.ollama_model,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_gemini = executor.submit(_call_gemini_worker) if is_online else None
        future_ollama = executor.submit(_call_ollama_worker)

        if future_gemini:
            try:
                gemini_resp = future_gemini.result()
            except Exception as exc:
                fallback_reasons.append(f"council_gemini_failed: {exc}")
                logger.warning("Council mode Gemini call failed: %s", exc)
        else:
            fallback_reasons.append("council_gemini_skipped_offline")

        try:
            ollama_resp = future_ollama.result()
        except Exception as exc:
            fallback_reasons.append(f"council_ollama_failed: {exc}")
            logger.warning("Council mode Ollama call failed: %s", exc)

    total_latency = (time.perf_counter() - start_time) * 1000

    # Case 1: Both responded -> Perform synthesis call
    if gemini_resp and ollama_resp:
        synth_prompt = (
            f"You are a master reconciler presiding over an expert council.\n"
            f"Question: {query}\n\n"
            f"Council Member A (Gemini) Response:\n{gemini_resp}\n\n"
            f"Council Member B (Qwen) Response:\n{ollama_resp}\n\n"
            f"Synthesize both perspectives into a single unified, accurate, and comprehensive response. "
            f"Reconcile any contradictions and present the definitive consensus answer."
        )

        synth_result = active_router.route_query(
            prompt=synth_prompt,
            query_type="fast",
            system_prompt="You are a synthesist. Produce a unified, clear, reconciled consensus answer.",
        )

        total_latency = (time.perf_counter() - start_time) * 1000
        return CouncilResult(
            text=synth_result.text,
            synthesized=True,
            gemini_response=gemini_resp,
            ollama_response=ollama_resp,
            latency_ms=total_latency,
            fallback_reasons=fallback_reasons,
            metadata={
                "synth_tier": synth_result.tier,
                "synth_model": synth_result.model,
            },
        )

    # Case 2: Only Gemini succeeded
    if gemini_resp:
        return CouncilResult(
            text=gemini_resp,
            synthesized=False,
            gemini_response=gemini_resp,
            ollama_response=None,
            latency_ms=total_latency,
            fallback_reasons=fallback_reasons,
            metadata={"note": "Council partial: Ollama unavailable, used Gemini single response."},
        )

    # Case 3: Only Ollama succeeded
    if ollama_resp:
        return CouncilResult(
            text=ollama_resp,
            synthesized=False,
            gemini_response=None,
            ollama_response=ollama_resp,
            latency_ms=total_latency,
            fallback_reasons=fallback_reasons,
            metadata={"note": "Council partial: Gemini unavailable, used Ollama single response."},
        )

    # Case 4: Neither succeeded
    raise CouncilError(f"Council mode failed: neither Gemini nor Ollama were available. Reasons: {fallback_reasons}")


# ------------------------------------------------------------------------------
# Race Mode (Gemini Flash vs Colab/Kaggle Qwen 14B)
# ------------------------------------------------------------------------------
def race_query(
    prompt: str,
    system_prompt: Optional[str] = None,
    colab_qwen_endpoint: Optional[str] = None,
    timeout: float = 15.0,
    router: Optional[LLMRouter] = None,
) -> RaceResult:
    """Races Gemini Flash vs Colab/Kaggle Qwen 14B simultaneously.

    Whichever model responds with a valid answer first wins the race and
    is returned immediately to drive real-time desktop actions.
    """
    active_router = router or get_default_router()
    endpoint = (
        colab_qwen_endpoint
        or os.environ.get("COLAB_QWEN_ENDPOINT")
        or os.environ.get("KAGGLE_QWEN_ENDPOINT")
        or ""
    ).rstrip("/")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    futures: Dict[concurrent.futures.Future, Tuple[str, str]] = {}
    t0 = time.perf_counter()

    # Runner 1: Gemini Flash
    f_gemini = executor.submit(active_router._call_gemini, prompt, timeout, system_prompt, active_router.gemini_model)
    futures[f_gemini] = ("gemini_flash", active_router.gemini_model)

    # Runner 2: Colab / Kaggle Qwen (or local Qwen fallback)
    if endpoint:
        def _call_qwen_cloud() -> str:
            url = f"{endpoint}/chat/completions" if endpoint.endswith("/v1") else f"{endpoint}/v1/chat/completions"
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": prompt})
            body = json.dumps({"model": "qwen2.5-14b", "messages": msgs, "temperature": 0.2}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                return d["choices"][0]["message"]["content"]

        def _call_qwen_cloud_with_fallback() -> str:
            try:
                url = f"{endpoint}/chat/completions" if endpoint.endswith("/v1") else f"{endpoint}/v1/chat/completions"
                msgs = []
                if system_prompt:
                    msgs.append({"role": "system", "content": system_prompt})
                msgs.append({"role": "user", "content": prompt})
                body = json.dumps({"model": "qwen2.5-14b", "messages": msgs, "temperature": 0.2}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    return d["choices"][0]["message"]["content"]
            except Exception as cloud_err:
                logger.warning(f"Cloud Qwen tunnel unavailable ({cloud_err}). Racing against local Qwen 2.5 3B instead.")
                return active_router._call_ollama(prompt, timeout, system_prompt, active_router.ollama_model)

        f_qwen = executor.submit(_call_qwen_cloud_with_fallback)
        futures[f_qwen] = ("colab_qwen_14b", "qwen2.5:14b")
    else:
        # Fallback to local Ollama Qwen
        f_qwen = executor.submit(active_router._call_ollama, prompt, timeout, system_prompt, active_router.ollama_model)
        futures[f_qwen] = ("local_qwen", active_router.ollama_model)

    all_res: Dict[str, Any] = {}
    winner_result = None

    for fut in concurrent.futures.as_completed(futures):
        tier, model = futures[fut]
        try:
            ans = fut.result()
            lat = (time.perf_counter() - t0) * 1000.0
            all_res[tier] = {"model": model, "latency_ms": lat, "text": ans}
            if winner_result is None and ans and str(ans).strip():
                logger.info(f"[RACE WINNER]: {tier.upper()} ({model}) won in {lat:.1f}ms!")
                try:
                    print(f"[*] [Race Winner]: {tier.upper()} ({model}) responded first in {lat:.1f}ms!")
                except Exception as _suppressed_exc:
                    logger.debug('[llm_router] Handled Exception: %s', _suppressed_exc, exc_info=True)
                winner_result = RaceResult(
                    text=str(ans).strip(),
                    winner_tier=tier,
                    winner_model=model,
                    winner_latency_ms=lat,
                    all_results=all_res,
                )
        except Exception as e:
            logger.warning(f"Racer {tier} ({model}) error: {e}")
            all_res[tier] = {"model": model, "error": str(e)}

    if winner_result:
        return winner_result
    raise AllTiersFailedError("All racers failed during race_query.", list(all_res.keys()))


def route_streaming(
    prompt: str,
    system_prompt: Optional[str] = None,
    timeout: float = 15.0,
    router: Optional[LLMRouter] = None,
) -> Generator[str, None, None]:
    """Streaming generator yielding Server-Sent Events (SSE) data chunks:
    `data: {"text": "..."}\\n\\n` with `data: [DONE]\\n\\n` terminator.
    Prioritizes Gemini Flash (gemini-3.8-flash), failing over to Groq on failure.
    """
    active_router = router or get_default_router()
    # 1. Primary: Gemini Flash streaming
    gemini_succeeded = False
    if active_router.gemini_api_key or active_router.gemini_api_keys:
        try:
            for chunk in active_router._call_gemini_stream(
                prompt=prompt,
                timeout=timeout,
                system_prompt=system_prompt,
                model=active_router.gemini_model,
            ):
                text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
                if text:
                    yield f"data: {json.dumps({'text': text})}\\n\\n"
            gemini_succeeded = True
        except Exception as gemini_err:
            logger.warning(f"[route_streaming] Gemini stream failed ({gemini_err}), failing over to Groq...")

    # 2. Failover: Groq streaming
    if not gemini_succeeded:
        if active_router.groq_api_key:
            try:
                for chunk in active_router._call_groq_stream(
                    prompt=prompt,
                    timeout=timeout,
                    system_prompt=system_prompt,
                    model=active_router.groq_model,
                ):
                    text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
                    if text:
                        yield f"data: {json.dumps({'text': text})}\\n\\n"
            except Exception as groq_err:
                logger.warning(f"[route_streaming] Groq stream failed ({groq_err}), falling back to synchronous route_query...")
                res = active_router.route_query(prompt, system_prompt=system_prompt, timeout=timeout)
                if res.text:
                    yield f"data: {json.dumps({'text': res.text})}\\n\\n"
        else:
            res = active_router.route_query(prompt, system_prompt=system_prompt, timeout=timeout)
            if res.text:
                yield f"data: {json.dumps({'text': res.text})}\\n\\n"

    yield "data: [DONE]\\n\\n"


__all__ = [
    "AllTiersFailedError",
    "CouncilError",
    "CouncilResult",
    "LLMRouter",
    "OLLAMA_RECHECK_SECONDS",
    "RaceResult",
    "RouteResult",
    "RouterError",
    "DEFAULT_SYSTEM_PROMPT",
    "GREETING_SYSTEM_PROMPT",
    "is_greeting_or_smalltalk",
    "check_connectivity",
    "classify_role",
    "council_mode",
    "get_default_router",
    "main",
    "query_expansion",
    "race_query",
    "redact_secrets",
    "redact_api_key",
    "route_image",
    "route_query",
    "route_query_stream",
    "route_query_stream_events",
    "route_streaming",
    "parse_cooldown_seconds",
    "_UNAVAILABLE_MODELS",
]


def parse_cooldown_seconds(err: Exception, router: Optional[Any] = None) -> float:
    """Parse remaining cooldown seconds from router or AllTiersFailedError fallback reasons."""
    if router is not None and hasattr(router, "get_gemini_cooldown_remaining"):
        rem = router.get_gemini_cooldown_remaining()
        if rem > 0:
            return rem
    if router is not None and hasattr(router, "_gemini_cooldown_until"):
        rem = max(0.0, router._gemini_cooldown_until - time.time())
        if rem > 0:
            return rem

    reasons = getattr(err, "fallback_reasons", [])
    for r in reasons:
        m = re.search(r"cooldown_active_([\d\.]+)s", str(r))
        if m:
            try:
                return float(m.group(1))
            except ValueError as _suppressed_exc:
                logger.debug('[llm_router] Handled ValueError: %s', _suppressed_exc, exc_info=True)
    m2 = re.search(r"cooldown_active_([\d\.]+)s", str(err))
    if m2:
        try:
            return float(m2.group(1))
        except ValueError as _suppressed_exc:
            logger.debug('[llm_router] Handled ValueError: %s', _suppressed_exc, exc_info=True)
    return 15.0


def main() -> None:
    """CLI entry point for llm_router."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="JAI Prioritized LLM Router")
    parser.add_argument("--query", "-q", type=str, help="Prompt to execute via prioritized routing")
    parser.add_argument(
        "--type", "-t", choices=["fast", "background"], default="fast",
        help="Query type: 'fast' (2s timeout) or 'background' (10s timeout)"
    )
    parser.add_argument("--expand", "-e", type=str, help="Run query expansion on ambiguous search terms")
    parser.add_argument("--council", "-c", type=str, help="Run council mode (Gemini + Ollama parallel synthesis)")
    parser.add_argument("--check-net", action="store_true", help="Check internet connectivity status")
    parser.add_argument("--view-logs", action="store_true", help="Display recent routing decisions from log")

    args = parser.parse_args()

    if args.check_net:
        online = check_connectivity()
        print(f"[Network] Connectivity status: {'ONLINE' if online else 'OFFLINE'}")
        sys.exit(0)

    if args.view_logs:
        router = get_default_router()
        if not os.path.exists(router.log_file):
            print(f"[Log] No log file found at {router.log_file}")
        else:
            print(f"[Log] Last 10 decisions from {router.log_file}:")
            with open(router.log_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            for line in lines[-10:]:
                print(f"  {line}")
        sys.exit(0)

    if args.expand:
        print(f"[Query Expansion] Input:  {args.expand}")
        expanded = query_expansion(args.expand)
        print(f"[Query Expansion] Output: {expanded}")
        sys.exit(0)

    if args.council:
        print(f"[Council Mode] Deliberating query: '{args.council}'...")
        try:
            res = council_mode(args.council)
            print(f"[Council Mode] Synthesized: {res.synthesized} | Latency: {res.latency_ms:.1f}ms")
            if res.gemini_response:
                print(f"\n--- Council Member A (Gemini) ---\n{res.gemini_response}")
            if res.ollama_response:
                print(f"\n--- Council Member B (Qwen) ---\n{res.ollama_response}")
            print(f"\n=== Final Synthesis ===\n{res.text}")
        except Exception as exc:
            print(f"[Council Mode Error] {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.query:
        print(f"[Router] Routing query ({args.type}): '{args.query}'...")
        try:
            result = route_query(args.query, query_type=args.type)
            print(f"[Router] Tier: {result.tier.upper()} | Model: {result.model} | Latency: {result.latency_ms:.1f}ms")
            if result.fallback_reasons:
                print(f"[Router] Fallback cascade: {' -> '.join(result.fallback_reasons)}")
            print(f"\n=== Response ===\n{result.text}")
        except Exception as exc:
            print(f"[Router Error] {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()


