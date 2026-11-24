"""Task 4 - Advanced async Python: a contact-centre compliance pipeline (DEMONSTRATION).

IMPORTANT - scope of this file
------------------------------
This is a self-contained *demonstration* written for this assessment. It is
modelled on the problem domain of the candidate's GhostAgent / DataRipple work
(call processing -> extraction -> compliance check -> CRM update), but it is
NOT production code from that project. All external systems (telephony
webhooks, LLM, CRM) are in-process mocks so the file runs offline.

What it demonstrates
--------------------
1. Provider adapters  - normalise Twilio / Vonage / RingCentral-style webhook
                        payloads (different shapes) into one validated model.
2. Async concurrency  - ``asyncio`` workers with a bounded ``Semaphore`` and a
                        per-call timeout (``asyncio.wait_for``).
3. Resilience         - retries with exponential backoff + full jitter, a
                        circuit breaker around the CRM, and a dead-letter queue.
4. Idempotency        - duplicate webhook deliveries are detected so the CRM is
                        never updated twice for the same call.
5. Business logic     - intent-specific required-document rules and prohibited
                        phrase detection (e.g. "guaranteed returns").
6. Security hygiene   - PII is masked before anything is logged; secrets would
                        be read from the environment, never hard-coded.

Run:  python advanced_python_example.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol, TypeVar

LOGGER = logging.getLogger("compliance_pipeline")
T = TypeVar("T")


# =========================================================================== #
# Domain model and validation
# =========================================================================== #
class Provider(str, Enum):
    """Supported telephony providers."""

    TWILIO = "twilio"
    VONAGE = "vonage"
    RINGCENTRAL = "ringcentral"


class Intent(str, Enum):
    """Customer intents the extractor can classify."""

    LOAN_APPLICATION = "loan_application"
    INSURANCE_QUOTE = "insurance_quote"
    ACCOUNT_SUPPORT = "account_support"
    UNKNOWN = "unknown"


class PayloadValidationError(ValueError):
    """Raised when a webhook payload cannot be normalised."""


E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


@dataclass(frozen=True)
class CallRecord:
    """Provider-independent, validated call record."""

    provider: Provider
    call_id: str
    customer_phone: str
    agent_id: str
    transcript: str
    documents_received: frozenset[str]

    @property
    def idempotency_key(self) -> str:
        """Stable key: same provider + call id -> same key across redeliveries."""
        return hashlib.sha256(f"{self.provider.value}:{self.call_id}".encode()).hexdigest()[:16]


def _require(payload: dict[str, Any], *path: str) -> Any:
    """Fetch a nested key or raise a clear validation error."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise PayloadValidationError(f"missing field '{'.'.join(path)}'")
        node = node[key]
    return node


def normalise_payload(provider: str, payload: dict[str, Any]) -> CallRecord:
    """Adapter layer: map each provider's webhook shape onto ``CallRecord``."""
    try:
        prov = Provider(provider)
    except ValueError as exc:
        raise PayloadValidationError(f"unsupported provider '{provider}'") from exc

    if prov is Provider.TWILIO:          # flat, PascalCase keys
        call_id = _require(payload, "CallSid")
        phone = _require(payload, "From")
        agent = _require(payload, "AgentId")
        transcript = _require(payload, "TranscriptionText")
        docs = payload.get("Documents", [])
    elif prov is Provider.VONAGE:        # snake_case with nested 'from'
        call_id = _require(payload, "uuid")
        phone = "+" + str(_require(payload, "from", "number")).lstrip("+")
        agent = _require(payload, "agent", "id")
        transcript = _require(payload, "transcript")
        docs = payload.get("attachments", [])
    else:                                # RingCentral: camelCase, nested session
        call_id = _require(payload, "session", "id")
        phone = _require(payload, "session", "caller", "phoneNumber")
        agent = _require(payload, "session", "agentExtension")
        transcript = " ".join(_require(payload, "utterances"))
        docs = payload.get("files", [])

    if not isinstance(transcript, str) or len(transcript.strip()) < 10:
        raise PayloadValidationError("transcript is empty or too short")
    if not E164_RE.fullmatch(str(phone)):
        raise PayloadValidationError("customer phone is not E.164")
    return CallRecord(prov, str(call_id), str(phone), str(agent), transcript.strip(),
                      frozenset(str(d).lower() for d in docs))


# =========================================================================== #
# PII masking (applied to everything that reaches logs)
# =========================================================================== #
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d \-]{7,}\d")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")  # Indian PAN format


def mask_pii(text: str) -> str:
    """Replace emails, phone numbers and PAN identifiers with placeholders."""
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PAN_RE.sub("[PAN]", text)
    return PHONE_RE.sub(lambda m: "[PHONE…" + re.sub(r"\D", "", m.group())[-2:] + "]", text)


# =========================================================================== #
# Extraction (an LLM in the real system; a deterministic mock here)
# =========================================================================== #
@dataclass
class Extraction:
    """Structured fields extracted from a transcript."""

    intent: Intent
    email: Optional[str]
    requirements: list[str]
    confidence: float


class Extractor(Protocol):
    """Interface so the mock can be swapped for a real LLM client."""

    async def extract(self, transcript: str) -> Extraction: ...


class TransientError(Exception):
    """A failure worth retrying (timeouts, 429, 5xx)."""


class MockLLMExtractor:
    """Keyword-based stand-in for an LLM with simulated latency and failures.

    A real implementation would call a model with a JSON schema / function-
    calling contract and validate the returned JSON exactly like ``Extraction``.
    """

    INTENT_KEYWORDS = {
        Intent.LOAN_APPLICATION: ("loan", "emi", "mortgage"),
        Intent.INSURANCE_QUOTE: ("insurance", "policy", "premium"),
        Intent.ACCOUNT_SUPPORT: ("password", "account", "statement"),
    }

    def __init__(self, rng: random.Random, failure_rate: float = 0.15) -> None:
        self.rng = rng
        self.failure_rate = failure_rate

    async def extract(self, transcript: str) -> Extraction:
        await asyncio.sleep(self.rng.uniform(0.01, 0.05))
        if self.rng.random() < self.failure_rate:
            raise TransientError("LLM provider returned 503")
        lowered = transcript.lower()
        scores = {i: sum(k in lowered for k in kws) for i, kws in self.INTENT_KEYWORDS.items()}
        intent, hits = max(scores.items(), key=lambda kv: kv[1])
        email_match = EMAIL_RE.search(transcript)
        reqs = [m.strip() for m in re.findall(r"(?:need|want|looking for)\s+([^.]+)", lowered)]
        return Extraction(intent if hits else Intent.UNKNOWN,
                          email_match.group() if email_match else None,
                          reqs, min(0.5 + 0.2 * hits, 0.95))


# =========================================================================== #
# Compliance rules (business logic)
# =========================================================================== #
REQUIRED_DOCUMENTS: dict[Intent, frozenset[str]] = {
    Intent.LOAN_APPLICATION: frozenset({"kyc_id", "income_proof", "consent_form"}),
    Intent.INSURANCE_QUOTE: frozenset({"kyc_id", "consent_form"}),
    Intent.ACCOUNT_SUPPORT: frozenset({"identity_verification"}),
    Intent.UNKNOWN: frozenset(),
}
PROHIBITED_PHRASES = ("guaranteed returns", "no risk at all", "100% approval")


@dataclass
class ComplianceResult:
    """Outcome of the compliance check for one call."""

    missing_documents: list[str]
    prohibited_phrases: list[str]
    needs_human_review: bool

    @property
    def compliant(self) -> bool:
        """True when nothing is missing and no prohibited phrase was used."""
        return not self.missing_documents and not self.prohibited_phrases


def evaluate_compliance(call: CallRecord, extraction: Extraction,
                        min_confidence: float = 0.6) -> ComplianceResult:
    """Apply intent-specific document rules and script checks."""
    missing = sorted(REQUIRED_DOCUMENTS[extraction.intent] - call.documents_received)
    lowered = call.transcript.lower()
    phrases = [p for p in PROHIBITED_PHRASES if p in lowered]
    # Low-confidence extractions or script violations go to a human, never auto-closed.
    review = bool(phrases) or extraction.confidence < min_confidence
    return ComplianceResult(missing, phrases, review)


# =========================================================================== #
# Resilience primitives
# =========================================================================== #
async def retry_async(fn: Callable[[], Awaitable[T]], *, attempts: int = 4,
                      base_delay: float = 0.02, max_delay: float = 0.5,
                      rng: Optional[random.Random] = None) -> T:
    """Retry ``fn`` on ``TransientError`` with exponential backoff and full jitter."""
    rng = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except TransientError:
            if attempt == attempts:
                raise
            delay = rng.uniform(0, min(max_delay, base_delay * 2 ** attempt))
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is rejecting calls."""


class CircuitBreaker:
    """Minimal async-safe circuit breaker (closed -> open -> half-open)."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 0.3) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.trips = 0
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` through the breaker."""
        async with self._lock:
            if self.opened_at is not None:
                if time.monotonic() - self.opened_at < self.reset_timeout:
                    raise CircuitOpenError("CRM circuit is open")
                self.opened_at = None  # half-open: allow a trial request
        try:
            result = await fn()
        except TransientError:
            async with self._lock:
                self.failures += 1
                if self.failures >= self.failure_threshold:
                    self.opened_at = time.monotonic()
                    self.failures = 0
                    self.trips += 1
            raise
        async with self._lock:
            self.failures = 0
        return result


# =========================================================================== #
# CRM client (mock) with idempotency
# =========================================================================== #
class MockCRMClient:
    """In-memory CRM that fails intermittently, like a rate-limited SaaS API."""

    def __init__(self, rng: random.Random, failure_rate: float = 0.2) -> None:
        # In production the token comes from a secrets manager / env var, never source.
        self.api_token = os.environ.get("CRM_API_TOKEN", "demo-not-a-secret")
        self.rng = rng
        self.failure_rate = failure_rate
        self.records: dict[str, dict[str, Any]] = {}

    async def upsert_contact(self, key: str, data: dict[str, Any]) -> str:
        """Create or update a contact; ``key`` makes the operation idempotent."""
        await asyncio.sleep(self.rng.uniform(0.005, 0.03))
        if self.rng.random() < self.failure_rate:
            raise TransientError("CRM returned 429 Too Many Requests")
        status = "updated" if key in self.records else "created"
        self.records[key] = data
        return status


# =========================================================================== #
# Pipeline orchestration
# =========================================================================== #
@dataclass
class PipelineStats:
    """Counters collected during a run."""

    outcomes: Counter = field(default_factory=Counter)
    dead_letters: list[tuple[str, str]] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)


class CompliancePipeline:
    """Validates, extracts, checks and syncs calls concurrently."""

    def __init__(self, extractor: Extractor, crm: MockCRMClient, *, concurrency: int = 8,
                 call_timeout: float = 1.5, seed: int = 7) -> None:
        self.extractor = extractor
        self.crm = crm
        self.breaker = CircuitBreaker()
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)
        self.call_timeout = call_timeout
        self.rng = random.Random(seed)
        self.processed_keys: set[str] = set()
        self._keys_lock = asyncio.Lock()
        self.stats = PipelineStats()

    async def _process(self, provider: str, payload: dict[str, Any]) -> str:
        call = normalise_payload(provider, payload)
        async with self._keys_lock:  # idempotency guard against webhook redelivery
            if call.idempotency_key in self.processed_keys:
                return "duplicate_skipped"
            self.processed_keys.add(call.idempotency_key)
        try:
            return await self._extract_check_sync(call)
        except BaseException:
            # Release the key so a later redelivery can retry this call.
            async with self._keys_lock:
                self.processed_keys.discard(call.idempotency_key)
            raise

    async def _extract_check_sync(self, call: CallRecord) -> str:
        extraction = await retry_async(lambda: self.extractor.extract(call.transcript),
                                       rng=self.rng)
        result = evaluate_compliance(call, extraction)
        crm_payload = {
            "phone": call.customer_phone, "email": extraction.email,
            "intent": extraction.intent.value, "requirements": extraction.requirements,
            "missing_documents": result.missing_documents,
            "compliance_status": "compliant" if result.compliant else "non_compliant",
            "needs_human_review": result.needs_human_review,
            "agent_id": call.agent_id,
        }
        await retry_async(lambda: self.breaker.call(
            lambda: self.crm.upsert_contact(call.idempotency_key, crm_payload)), rng=self.rng)
        LOGGER.info("call=%s intent=%s compliant=%s transcript=%s", call.call_id,
                    extraction.intent.value, result.compliant, mask_pii(call.transcript[:80]))
        if result.needs_human_review:
            return "human_review"
        return "compliant" if result.compliant else "non_compliant"

    async def handle(self, provider: str, payload: dict[str, Any]) -> str:
        """Process one webhook with bounded concurrency, timeout and DLQ fallback."""
        async with self.semaphore:
            start = time.perf_counter()
            ref = str(payload.get("CallSid") or payload.get("uuid")
                      or payload.get("session", {}).get("id") or "?")
            try:
                outcome = await asyncio.wait_for(self._process(provider, payload),
                                                 timeout=self.call_timeout)
            except PayloadValidationError as exc:
                outcome = "rejected_invalid"
                self.stats.dead_letters.append((ref, f"validation: {exc}"))
            except (TransientError, CircuitOpenError, asyncio.TimeoutError) as exc:
                outcome = "dead_lettered"
                self.stats.dead_letters.append((ref, f"{type(exc).__name__}: {exc}"))
            self.stats.outcomes[outcome] += 1
            self.stats.durations.append(time.perf_counter() - start)
            return outcome

    async def run(self, events: list[tuple[str, dict[str, Any]]]) -> PipelineStats:
        """Process all events concurrently; exceptions never escape a single task."""
        await asyncio.gather(*(self.handle(p, e) for p, e in events))
        return self.stats


# =========================================================================== #
# Synthetic workload
# =========================================================================== #
TRANSCRIPTS = [
    "Hi, I need a home loan of 40 lakhs, my email is ravi.k@example.com. What EMI options exist?",
    "I am looking for health insurance for my parents, what is the premium? PAN ABCDE1234F.",
    "I forgot my account password and need a statement for last month.",
    "This plan has guaranteed returns and no risk at all, sign the loan papers today.",
    "Just calling to say thanks for the help yesterday, nothing else needed today.",
]
DOC_SETS = [[], ["kyc_id"], ["kyc_id", "consent_form"],
            ["kyc_id", "income_proof", "consent_form"], ["identity_verification"]]


def make_events(n: int, rng: random.Random) -> list[tuple[str, dict[str, Any]]]:
    """Generate ``n`` synthetic webhooks across providers, plus bad/duplicate ones."""
    events: list[tuple[str, dict[str, Any]]] = []
    for i in range(n):
        text, docs = rng.choice(TRANSCRIPTS), rng.choice(DOC_SETS)
        phone = f"+9198{rng.randint(10_000_000, 99_999_999)}"
        provider = rng.choice(list(Provider)).value
        if provider == "twilio":
            payload = {"CallSid": f"CA{i:05d}", "From": phone, "AgentId": f"A{i % 12}",
                       "TranscriptionText": text, "Documents": docs}
        elif provider == "vonage":
            payload = {"uuid": f"vn-{i:05d}", "from": {"number": phone.lstrip("+")},
                       "agent": {"id": f"A{i % 12}"}, "transcript": text, "attachments": docs}
        else:
            payload = {"session": {"id": f"rc-{i:05d}", "caller": {"phoneNumber": phone},
                                   "agentExtension": f"A{i % 12}"},
                       "utterances": text.split(". "), "files": docs}
        events.append((provider, payload))
    events += events[:5]  # simulate webhook redelivery
    events.append(("twilio", {"CallSid": "CAbad1", "From": "12345", "AgentId": "A1",
                              "TranscriptionText": "too short"}))
    events.append(("zoom", {"id": "z1"}))
    return events


async def main_async(n_calls: int = 60, seed: int = 7) -> PipelineStats:
    """Build the pipeline, run the workload and print a report."""
    rng = random.Random(seed)
    pipeline = CompliancePipeline(MockLLMExtractor(rng), MockCRMClient(rng), seed=seed)
    events = make_events(n_calls, rng)
    started = time.perf_counter()
    stats = await pipeline.run(events)
    elapsed = time.perf_counter() - started

    print(f"Processed {len(events)} webhook events in {elapsed:.2f}s "
          f"(concurrency={pipeline.concurrency})")
    for outcome, count in sorted(stats.outcomes.items()):
        print(f"  {outcome:<18} {count}")
    print(f"CRM records stored: {len(pipeline.crm.records)}  |  "
          f"circuit-breaker trips: {pipeline.breaker.trips}")
    print(f"Dead-letter queue ({len(stats.dead_letters)}):")
    for ref, reason in stats.dead_letters[:6]:
        print(f"  {ref:<10} {reason}")
    print("PII masking example:", mask_pii(TRANSCRIPTS[0] + " " + TRANSCRIPTS[1]))
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main_async())
