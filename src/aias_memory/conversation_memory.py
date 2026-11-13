"""
Conversation Memory Service

Provides session-scoped fact extraction and retrieval
for conversation-aware AI responses.

Architecture: Two-lane memory system
- Lane 1: Short-term buffer (last N turns, verbatim)
- Lane 2: Session memory (extracted facts, preferences, constraints)
"""

import json
import hashlib
import asyncio
import re
import time
import os
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)


class MemoryScope(Enum):
    USER = "user"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    LEAD = "lead"


class EvictionStrategy(Enum):
    OLDEST_FIRST = "oldest_first"
    LOWEST_CONFIDENCE = "lowest_confidence"
    LRU = "lru"


@dataclass
class SessionFact:
    content: str
    category: str
    confidence: float
    turn_number: int
    created_at: datetime
    content_hash: Optional[str] = None
    last_accessed: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "category": self.category,
            "confidence": self.confidence,
            "turn_number": self.turn_number,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "content_hash": self.content_hash,
            "last_accessed": self.last_accessed.isoformat() if self.last_accessed else None
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionFact":
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.utcnow()
            
        last_accessed = data.get("last_accessed")
        if isinstance(last_accessed, str):
            last_accessed = datetime.fromisoformat(last_accessed)
            
        return cls(
            content=data["content"],
            category=data["category"],
            confidence=float(data["confidence"]),
            turn_number=int(data["turn_number"]),
            created_at=created_at,
            content_hash=data.get("content_hash"),
            last_accessed=last_accessed
        )


@dataclass
class SessionConfig:
    scope: MemoryScope = MemoryScope.USER
    memory_enabled: bool = True
    compression_enabled: bool = True


class MemoryCircuitBreaker:
    """Prevent cascading failures in memory operations"""
    
    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure_time: Optional[float] = None
        self.state = "closed"
    
    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        
        if self.state == "open":
            if self.last_failure_time and time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        
        return True
    
    def record_success(self):
        self.failure_count = 0
        self.state = "closed"
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning("Memory circuit breaker opened - disabling memory ops temporarily")


EXTRACTION_PROMPT = """You are a context extraction system. Analyze the conversation turn and extract factual information that should be remembered for future responses.

Extract ONLY concrete facts, preferences, constraints, or decisions. Do NOT extract:
- Greetings or pleasantries
- Questions (extract answers instead)
- Vague statements
- Information already implied by the assistant

PRIVACY RULES:
- Do NOT extract email addresses, phone numbers, or URLs
- Do NOT extract names of individuals (company names OK)
- Do NOT extract financial figures or account numbers
- Extract preferences abstractly: "prefers email contact" not "email is john@..."

Output format (one per line):
CATEGORY|CONFIDENCE|FACT

Categories:
- preference: User's stated preferences
- constraint: Limitations or requirements
- decision: Choices that have been made
- context: Background information

Example output:
preference|0.9|User prefers Python for backend development
constraint|0.95|Budget is limited to $5,000
decision|0.85|Will use PostgreSQL instead of MongoDB

If no extractable facts, output: NONE

Analyze this turn:"""


PII_PATTERNS = [
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',
    r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b',
    r'\b\d{3}-\d{2}-\d{4}\b',
]


FACT_PRIORITY = {
    "decision": 1,
    "constraint": 2,
    "preference": 3,
    "context": 4
}


CONTEXT_BUDGETS = {
    "system_directives": 0.20,
    "session_memory": 0.15,
    "short_term_buffer": 0.55,
    "user_question": 0.10
}


class ConversationMemory:
    """Provider-agnostic conversation memory management"""
    
    EXTRACTION_MODELS = {
        "groq": "llama-3.1-8b-instant",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-haiku-20240307",
        "gemini": "gemini-1.5-flash",
        "mistral": "mistral-small-latest"
    }
    
    CONTEXT_LIMITS = {
        "groq": 8000,
        "openai": 12000,
        "anthropic": 16000,
        "gemini": 20000,
        "mistral": 8000
    }
    
    MAX_FACTS_PER_SESSION = 15
    SESSION_TTL = 86400 * 7
    CONFIDENCE_THRESHOLD = 0.75
    EXTRACTION_TIMEOUT = 5.0
    
    def __init__(self, redis_client=None):
        self.r = redis_client
        self.circuit_breaker = MemoryCircuitBreaker()
        self._kill_switch_cache: Optional[bool] = None
        self._kill_switch_cache_time: float = 0
    
    def set_redis(self, redis_client):
        """Set Redis client after initialization"""
        self.r = redis_client

    @staticmethod
    def get_session_id(
        workspace_id: str,
        user_id: Optional[str],
        conversation_id: str,
        lead_id: Optional[str],
        scope: MemoryScope
    ) -> str:
        """Generate session ID based on configured scope"""
        if scope == MemoryScope.USER and user_id:
            return f"{workspace_id}:{user_id}"
        elif scope == MemoryScope.WORKSPACE:
            return f"{workspace_id}:shared"
        elif scope == MemoryScope.CONVERSATION:
            return f"{workspace_id}:{conversation_id}"
        elif scope == MemoryScope.LEAD and lead_id:
            return f"{workspace_id}:lead:{lead_id}"
        else:
            return f"{workspace_id}:{conversation_id}"

    @staticmethod
    def resolve_memory_scope(
        user_id: Optional[str],
        lead_id: Optional[str],
        conversation_id: str,
        workspace_scope: MemoryScope = MemoryScope.USER
    ) -> Tuple[MemoryScope, str]:
        """
        Determine memory scope based on authentication state.
        
        Priority:
        1. Registered user → USER scope
        2. Captured lead → LEAD scope  
        3. Anonymous (no email) → CONVERSATION scope (ephemeral)
        """
        if user_id:
            return (MemoryScope.USER, user_id)
        elif lead_id:
            return (MemoryScope.LEAD, lead_id)
        else:
            return (MemoryScope.CONVERSATION, conversation_id)

    def _facts_key(self, session_id: str) -> str:
        return f"aai:session:{session_id}:facts"
    
    def _meta_key(self, session_id: str) -> str:
        return f"aai:session:{session_id}:meta"

    async def get_session_facts(self, session_id: str) -> List[SessionFact]:
        """Retrieve session facts with graceful failure handling"""
        if not self.r:
            return []
            
        if not self.circuit_breaker.can_execute():
            return []
        
        try:
            facts_data = await asyncio.wait_for(
                self._get_session_facts_internal(session_id),
                timeout=2.0
            )
            self.circuit_breaker.record_success()
            return facts_data
        
        except asyncio.TimeoutError:
            logger.warning(f"Session facts retrieval timed out for {session_id}")
            self.circuit_breaker.record_failure()
            return []
        
        except Exception as e:
            logger.error(f"Error retrieving session facts: {e}")
            self.circuit_breaker.record_failure()
            return []

    async def _get_session_facts_internal(self, session_id: str) -> List[SessionFact]:
        """Internal method to get facts from Redis"""
        facts_key = self._facts_key(session_id)
        
        raw_facts = self.r.hgetall(facts_key)
        if not raw_facts:
            return []
        
        facts = []
        for key, value in raw_facts.items():
            if key.startswith("fact:"):
                try:
                    fact_data = json.loads(value)
                    facts.append(SessionFact.from_dict(fact_data))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse fact {key}: {e}")
                    continue
        
        await self.touch_session(session_id)
        return facts

    async def add_session_facts(self, session_id: str, new_facts: List[SessionFact]) -> int:
        """Add facts with deduplication and eviction. Returns count of facts added."""
        if not self.r or not new_facts:
            return 0
        
        facts_key = self._facts_key(session_id)
        existing_hashes = await self._get_existing_hashes(session_id)
        
        added_count = 0
        for fact in new_facts:
            fact.content_hash = self._compute_content_hash(fact.content)
            
            if fact.content_hash in existing_hashes:
                await self._refresh_fact_metadata(session_id, fact)
                logger.debug(f"Refreshed existing fact: {fact.content[:50]}...")
            else:
                current_count = len(existing_hashes)
                if current_count >= self.MAX_FACTS_PER_SESSION:
                    await self._evict_facts(session_id, 1)
                
                fact_index = self.r.hincrby(facts_key, "fact_count", 1) - 1
                self.r.hset(facts_key, f"fact:{fact_index}", json.dumps(fact.to_dict()))
                existing_hashes.add(fact.content_hash)
                added_count += 1
        
        self.r.hset(facts_key, "last_updated", datetime.utcnow().isoformat())
        await self.touch_session(session_id)
        
        return added_count

    async def _get_existing_hashes(self, session_id: str) -> set:
        """Get set of existing content hashes for deduplication"""
        facts = await self._get_session_facts_internal(session_id)
        return {f.content_hash for f in facts if f.content_hash}

    async def _refresh_fact_metadata(self, session_id: str, fact: SessionFact):
        """Update existing fact's metadata (turn number, timestamp)"""
        facts_key = self._facts_key(session_id)
        raw_facts = self.r.hgetall(facts_key)
        
        for key, value in raw_facts.items():
            if key.startswith("fact:"):
                try:
                    stored_fact = json.loads(value)
                    if stored_fact.get("content_hash") == fact.content_hash:
                        stored_fact["turn_number"] = fact.turn_number
                        stored_fact["last_accessed"] = datetime.utcnow().isoformat()
                        self.r.hset(facts_key, key, json.dumps(stored_fact))
                        break
                except json.JSONDecodeError:
                    continue

    async def _evict_facts(self, session_id: str, count: int):
        """Evict oldest facts to make room for new ones"""
        facts = await self._get_session_facts_internal(session_id)
        if not facts:
            return
        
        facts.sort(key=lambda f: f.turn_number)
        to_evict = facts[:count]
        
        facts_key = self._facts_key(session_id)
        raw_facts = self.r.hgetall(facts_key)
        
        evict_hashes = {f.content_hash for f in to_evict}
        for key, value in raw_facts.items():
            if key.startswith("fact:"):
                try:
                    stored_fact = json.loads(value)
                    if stored_fact.get("content_hash") in evict_hashes:
                        self.r.hdel(facts_key, key)
                        logger.debug(f"Evicted fact: {stored_fact.get('content', '')[:50]}...")
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        """Generate hash for deduplication"""
        normalized = content.lower().strip()
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    async def touch_session(self, session_id: str, org_id: Optional[str] = None):
        """Refresh TTL on session access"""
        if not self.r:
            return
            
        ttl = self.SESSION_TTL
        for suffix in ["facts", "meta"]:
            key = f"aai:session:{session_id}:{suffix}"
            self.r.expire(key, ttl)

    def format_session_memory(self, facts: List[SessionFact]) -> str:
        """Format facts for injection, sorted by priority then recency."""
        if not facts:
            return ""
        
        sorted_facts = sorted(
            facts,
            key=lambda f: (FACT_PRIORITY.get(f.category, 99), -f.turn_number)
        )
        
        lines = ["[Session Context]"]
        for fact in sorted_facts:
            lines.append(f"- {fact.content}")
        return "\n".join(lines)

    async def delete_session_fact(self, session_id: str, content_hash: str) -> bool:
        """Delete a specific fact by its content hash. Returns True if deleted."""
        if not self.r:
            return False
        
        facts_key = self._facts_key(session_id)
        raw_facts = self.r.hgetall(facts_key)
        
        for key, value in raw_facts.items():
            if key.startswith("fact:"):
                try:
                    stored_fact = json.loads(value)
                    if stored_fact.get("content_hash") == content_hash:
                        self.r.hdel(facts_key, key)
                        logger.info(f"Deleted fact {content_hash} from session {session_id}")
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    async def update_session_fact(self, session_id: str, content_hash: str, new_content: str) -> Optional[SessionFact]:
        """Update a fact's content. Returns updated fact or None if not found."""
        if not self.r:
            return None
        
        facts_key = self._facts_key(session_id)
        raw_facts = self.r.hgetall(facts_key)
        
        for fact_key, value in raw_facts.items():
            if fact_key.startswith("fact:"):
                try:
                    stored_fact = json.loads(value)
                    if stored_fact.get("content_hash") == content_hash:
                        stored_fact["content"] = new_content
                        stored_fact["content_hash"] = self._compute_content_hash(new_content)
                        stored_fact["last_accessed"] = datetime.utcnow().isoformat()
                        self.r.hset(facts_key, fact_key, json.dumps(stored_fact))
                        logger.info(f"Updated fact {content_hash} in session {session_id}")
                        return SessionFact.from_dict(stored_fact)
                except json.JSONDecodeError:
                    continue
        return None

    async def clear_session_facts(self, session_id: str) -> int:
        """Clear all facts from a session. Returns count deleted."""
        if not self.r:
            return 0
        
        facts_key = self._facts_key(session_id)
        raw_facts = self.r.hgetall(facts_key)
        
        deleted = 0
        for key in list(raw_facts.keys()):
            if key.startswith("fact:"):
                self.r.hdel(facts_key, key)
                deleted += 1
        
        self.r.hset(facts_key, "fact_count", "0")
        self.r.hset(facts_key, "last_updated", datetime.utcnow().isoformat())
        logger.info(f"Cleared {deleted} facts from session {session_id}")
        return deleted

    def calculate_token_budget(self, provider: str, memory_enabled: bool = True) -> Dict[str, int]:
        """Calculate token budgets for each prompt section"""
        total = self.CONTEXT_LIMITS.get(provider, 8000)
        
        budgets = CONTEXT_BUDGETS.copy()
        
        if not memory_enabled:
            budgets["short_term_buffer"] += budgets["session_memory"]
            budgets["session_memory"] = 0
        
        return {
            section: int(total * ratio)
            for section, ratio in budgets.items()
        }

    def _parse_facts(self, raw_output: str, turn_number: int = 0) -> List[SessionFact]:
        """Parse extraction output into structured facts"""
        if not raw_output or raw_output.strip().upper() == "NONE":
            return []
        
        facts = []
        for line in raw_output.strip().split("\n"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                category, confidence, content = parts
                try:
                    conf_value = float(confidence.strip())
                    if conf_value < 0 or conf_value > 1:
                        continue
                        
                    facts.append(SessionFact(
                        content=content.strip(),
                        category=category.strip().lower(),
                        confidence=conf_value,
                        turn_number=turn_number,
                        created_at=datetime.utcnow()
                    ))
                except (ValueError, AttributeError):
                    continue
        return facts

    @staticmethod
    def filter_by_confidence(facts: List[SessionFact], threshold: float = 0.75) -> List[SessionFact]:
        """Only persist high-confidence facts"""
        filtered = [f for f in facts if f.confidence >= threshold]
        if len(filtered) < len(facts):
            logger.debug(f"Filtered {len(facts) - len(filtered)} low-confidence facts")
        return filtered

    @staticmethod
    def filter_pii(facts: List[SessionFact]) -> List[SessionFact]:
        """Remove facts that contain PII patterns"""
        filtered = []
        for fact in facts:
            has_pii = any(re.search(pattern, fact.content) for pattern in PII_PATTERNS)
            if not has_pii:
                filtered.append(fact)
            else:
                logger.info("Filtered fact containing PII pattern")
        return filtered

    async def is_extraction_enabled(self) -> bool:
        """Check if extraction is enabled (kill switch not active)"""
        env_kill = os.getenv("MEMORY_EXTRACTION_DISABLED", "false").lower() == "true"
        if env_kill:
            return False
        
        if not self.r:
            return True
        
        if time.time() - self._kill_switch_cache_time < 30:
            return not self._kill_switch_cache if self._kill_switch_cache is not None else True
        
        try:
            disabled = self.r.get("aai:config:extraction_disabled")
            self._kill_switch_cache = bool(disabled)
            self._kill_switch_cache_time = time.time()
            return not disabled
        except Exception:
            return True

    async def set_extraction_enabled(self, enabled: bool):
        """Toggle extraction at runtime"""
        if not self.r:
            return
            
        if enabled:
            self.r.delete("aai:config:extraction_disabled")
        else:
            self.r.set("aai:config:extraction_disabled", "1")
        
        self._kill_switch_cache = not enabled
        self._kill_switch_cache_time = time.time()

    async def extract_facts_openai_compatible(
        self,
        client,
        model: str,
        user_msg: str,
        ai_response: str
    ) -> List[SessionFact]:
        """Extract facts using OpenAI-compatible API (Groq, OpenAI, Mistral)"""
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": f"USER: {user_msg}\nASSISTANT: {ai_response}"}
                ],
                temperature=0.3,
                max_tokens=256
            )
            return self._parse_facts(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"OpenAI-compatible extraction failed: {e}")
            return []

    async def extract_facts_anthropic(
        self,
        client,
        model: str,
        user_msg: str,
        ai_response: str
    ) -> List[SessionFact]:
        """Extract facts using Anthropic API"""
        try:
            response = client.messages.create(
                model=model,
                system=EXTRACTION_PROMPT,
                messages=[
                    {"role": "user", "content": f"USER: {user_msg}\nASSISTANT: {ai_response}"}
                ],
                temperature=0.3,
                max_tokens=256
            )
            return self._parse_facts(response.content[0].text)
        except Exception as e:
            logger.warning(f"Anthropic extraction failed: {e}")
            return []

    async def extract_facts_gemini(
        self,
        genai_module,
        model: str,
        user_msg: str,
        ai_response: str
    ) -> List[SessionFact]:
        """Extract facts using Google Gemini API"""
        try:
            gmodel = genai_module.GenerativeModel(model)
            combined = f"{EXTRACTION_PROMPT}\n\nUSER: {user_msg}\nASSISTANT: {ai_response}"
            response = gmodel.generate_content(combined)
            return self._parse_facts(response.text)
        except Exception as e:
            logger.warning(f"Gemini extraction failed: {e}")
            return []

    async def extract_facts(
        self,
        provider: str,
        client,
        user_msg: str,
        ai_response: str,
        turn_number: int = 0
    ) -> List[SessionFact]:
        """Route to appropriate provider extraction method"""
        model = self.EXTRACTION_MODELS.get(provider)
        if not model:
            return []
        
        if provider in ["openai", "groq", "mistral"]:
            facts = await self.extract_facts_openai_compatible(client, model, user_msg, ai_response)
        elif provider == "anthropic":
            facts = await self.extract_facts_anthropic(client, model, user_msg, ai_response)
        elif provider == "gemini":
            facts = await self.extract_facts_gemini(client, model, user_msg, ai_response)
        else:
            return []
        
        for fact in facts:
            fact.turn_number = turn_number
        
        return facts

    async def post_response_extraction(
        self,
        session_id: str,
        user_msg: str,
        ai_response: str,
        provider: str,
        client,
        turn_number: int = 0
    ):
        """
        Async extraction flow with all safety measures:
        - Kill-switch check
        - Confidence threshold filtering
        - PII filtering
        - Deduplication (in add_session_facts)
        """
        if not await self.is_extraction_enabled():
            logger.debug("Extraction disabled via kill switch")
            return
        
        try:
            facts = await asyncio.wait_for(
                self.extract_facts(provider, client, user_msg, ai_response, turn_number),
                timeout=self.EXTRACTION_TIMEOUT
            )
            
            if facts:
                facts = self.filter_by_confidence(facts, self.CONFIDENCE_THRESHOLD)
                facts = self.filter_pii(facts)
                
                if facts:
                    added = await self.add_session_facts(session_id, facts)
                    logger.debug(f"Added {added} facts to session {session_id}")
                    
        except asyncio.TimeoutError:
            logger.warning(f"Extraction timed out for session {session_id}")
        except Exception as e:
            logger.warning(f"Fact extraction failed for session {session_id}: {e}")

    async def purge_session(self, session_id: str, reason: str = "user_request"):
        """Completely remove all session data"""
        if not self.r:
            return
            
        keys_to_delete = [
            self._facts_key(session_id),
            self._meta_key(session_id)
        ]
        
        for key in keys_to_delete:
            self.r.delete(key)
        
        logger.info(f"Purged session {session_id}, reason: {reason}")

    async def purge_workspace_sessions(self, workspace_id: str):
        """Remove all session facts when memory is disabled for workspace"""
        if not self.r:
            return
            
        pattern = f"aai:session:{workspace_id}:*"
        keys = list(self.r.scan_iter(match=pattern))
        
        if keys:
            for key in keys:
                self.r.delete(key)
            logger.info(f"Purged {len(keys)} session keys for workspace {workspace_id}")

    async def migrate_lead_to_user(self, lead_id: str, user_id: str, workspace_id: str):
        """
        When a lead converts to registered user, migrate their memory.
        """
        if not self.r:
            return
            
        lead_session_id = f"{workspace_id}:lead:{lead_id}"
        user_session_id = f"{workspace_id}:{user_id}"
        
        lead_facts = await self._get_session_facts_internal(lead_session_id)
        
        if lead_facts:
            await self.add_session_facts(user_session_id, lead_facts)
            await self.purge_session(lead_session_id, reason="lead_to_user_migration")
            logger.info(f"Migrated {len(lead_facts)} facts from lead:{lead_id} to user:{user_id}")

    async def export_session_data(self, session_id: str) -> dict:
        """Export all session data for user data request"""
        facts = await self.get_session_facts(session_id)
        
        meta_key = self._meta_key(session_id)
        meta = self.r.hgetall(meta_key) if self.r else {}
        
        return {
            "session_id": session_id,
            "created_at": meta.get("created_at"),
            "last_activity": meta.get("last_activity"),
            "facts": [
                {
                    "content": f.content,
                    "category": f.category,
                    "extracted_at": f.created_at.isoformat() if isinstance(f.created_at, datetime) else f.created_at
                }
                for f in facts
            ]
        }

    def _estimate_tokens(self, messages: list) -> int:
        """Rough token estimation (4 chars ≈ 1 token)"""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars // 4

    async def compress_buffer(
        self,
        messages: list,
        max_tokens: int,
        provider: str,
        client=None,
        compression_enabled: bool = True
    ) -> list:
        """
        Compress older messages while preserving recent ones.
        
        When compression_enabled=False: truncate instead of summarize (for compliance/audit)
        """
        PRESERVE_RECENT = 4
        
        if len(messages) <= PRESERVE_RECENT:
            return messages
        
        recent = messages[-PRESERVE_RECENT:]
        older = messages[:-PRESERVE_RECENT]
        
        recent_tokens = self._estimate_tokens(recent)
        remaining_budget = max_tokens - recent_tokens
        
        if remaining_budget <= 0:
            return recent
        
        older_tokens = self._estimate_tokens(older)
        if older_tokens <= remaining_budget:
            return messages
        
        if not compression_enabled or not client:
            turns_to_keep = max(1, int(len(older) * (remaining_budget / older_tokens)))
            return older[-turns_to_keep:] + recent
        
        try:
            summary = await self._summarize_messages(older, provider, client)
            if summary:
                return [{"role": "system", "content": f"[Earlier conversation summary: {summary}]"}] + recent
        except Exception as e:
            logger.warning(f"Buffer compression failed: {e}")
        
        return recent

    async def _summarize_messages(self, messages: list, provider: str, client) -> str:
        """Summarize older messages using a cheap model"""
        model = self.EXTRACTION_MODELS.get(provider)
        if not model or not client:
            return ""
        
        transcript = "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages])
        
        summary_prompt = """Summarize this conversation excerpt in 2-3 sentences, focusing on:
- Key topics discussed
- Decisions made
- Important context for continuing the conversation

Do not include greetings or pleasantries. Be concise."""
        
        try:
            if provider in ["openai", "groq", "mistral"]:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": summary_prompt},
                        {"role": "user", "content": transcript}
                    ],
                    temperature=0.3,
                    max_tokens=150
                )
                return response.choices[0].message.content.strip()
            elif provider == "anthropic":
                response = client.messages.create(
                    model=model,
                    system=summary_prompt,
                    messages=[{"role": "user", "content": transcript}],
                    temperature=0.3,
                    max_tokens=150
                )
                return response.content[0].text.strip()
        except Exception as e:
            logger.warning(f"Message summarization failed: {e}")
        
        return ""

    async def update_session_metadata(
        self,
        session_id: str,
        workspace_id: str,
        user_id: Optional[str],
        provider: str
    ):
        """Update session metadata on each interaction"""
        if not self.r:
            return
        
        meta_key = self._meta_key(session_id)
        now = datetime.utcnow().isoformat()
        
        existing = self.r.hgetall(meta_key)
        if not existing:
            self.r.hset(meta_key, mapping={
                "workspace_id": workspace_id,
                "user_id": user_id or "",
                "created_at": now,
                "last_activity": now,
                "total_turns": "1",
                "provider": provider,
                "extraction_enabled": "true"
            })
        else:
            current_turns = int(existing.get("total_turns", "0"))
            self.r.hset(meta_key, mapping={
                "last_activity": now,
                "total_turns": str(current_turns + 1),
                "provider": provider
            })
        
        await self.touch_session(session_id)

    async def extract_with_rate_limit_handling(
        self,
        provider: str,
        client,
        user_msg: str,
        ai_response: str,
        turn_number: int = 0,
        max_retries: int = 2
    ) -> List[SessionFact]:
        """Extract facts with exponential backoff for rate limits"""
        import random
        
        for attempt in range(max_retries + 1):
            try:
                return await self.extract_facts(provider, client, user_msg, ai_response, turn_number)
            except Exception as e:
                error_str = str(e).lower()
                if "rate" in error_str or "429" in error_str or "limit" in error_str:
                    if attempt == max_retries:
                        logger.warning(f"Extraction rate limited after {max_retries} retries")
                        return []
                    
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.debug(f"Rate limited, waiting {wait_time:.1f}s before retry")
                    await asyncio.sleep(wait_time)
                else:
                    raise
        
        return []

    def get_effective_extraction_provider(self, default_provider: str) -> str:
        """Get effective provider for extraction (supports reroute override)"""
        reroute = os.getenv("MEMORY_EXTRACTION_PROVIDER")
        if reroute and reroute in self.EXTRACTION_MODELS:
            logger.debug(f"Extraction rerouted from {default_provider} to {reroute}")
            return reroute
        return default_provider

    async def get_session_stats(self, session_id: str) -> dict:
        """Get session statistics for monitoring"""
        if not self.r:
            return {}
        
        facts = await self.get_session_facts(session_id)
        meta_key = self._meta_key(session_id)
        meta = self.r.hgetall(meta_key) or {}
        
        return {
            "session_id": session_id,
            "fact_count": len(facts),
            "total_turns": int(meta.get("total_turns", "0")),
            "created_at": meta.get("created_at"),
            "last_activity": meta.get("last_activity"),
            "provider": meta.get("provider"),
            "categories": {
                cat: len([f for f in facts if f.category == cat])
                for cat in ["decision", "constraint", "preference", "context"]
            }
        }

    async def get_memory_stats_for_workspace(self, workspace_id: str) -> dict:
        """Get aggregate memory stats for a workspace (admin dashboard)"""
        if not self.r:
            return {"error": "Redis not available"}
        
        pattern = f"aai:session:{workspace_id}:*:facts"
        keys = list(self.r.scan_iter(match=pattern, count=100))
        
        total_facts = 0
        session_count = len(keys)
        
        for key in keys[:50]:
            try:
                fact_count = self.r.hget(key, "fact_count")
                if fact_count:
                    total_facts += int(fact_count)
            except Exception:
                continue
        
        return {
            "workspace_id": workspace_id,
            "active_sessions": session_count,
            "total_facts": total_facts,
            "extraction_enabled": await self.is_extraction_enabled()
        }


MEMORY_VISIBILITY_GUARD = """
MEMORY VISIBILITY RULES:
- Do not mention "I remember that..." or "Based on our previous conversation..."
- Do not list stored preferences or constraints unless the user asks
- Reference context naturally without citing the memory system
- Never reveal confidence levels or extraction metadata
"""


conversation_memory = ConversationMemory()
