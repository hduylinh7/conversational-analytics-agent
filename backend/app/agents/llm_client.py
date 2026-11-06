"""LLM Client implementations supporting OpenRouter, OpenAI, and deterministic Mock testing."""

from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    """Base exception for LLM generation failures."""

    pass


class BaseLLMClient(ABC):
    """Abstract base class for LLM client providers."""

    @abstractmethod
    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Synchronously generate a text response from conversational messages."""
        pass

    @abstractmethod
    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Asynchronously generate a text response from conversational messages."""
        pass


class OpenRouterClient(BaseLLMClient):
    """Client for OpenRouter LLM gateway using httpx."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
    ) -> None:
        """Initialize OpenRouter client with API credentials and model configuration.

        Args:
            api_key: OpenRouter API key. If None, derived from Settings.
            model: Model identifier. If None, derived from Settings.
            base_url: Base endpoint URL. If None, defaults to 'https://openrouter.ai/api/v1'.
            temperature: Sampling temperature.
            timeout: HTTP request timeout in seconds.
        """
        settings = get_settings()
        self.api_key = api_key or settings.OPENROUTER_API_KEY or settings.OPENAI_API_KEY
        self.model = model or settings.LLM_MODEL
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

        if not self.api_key:
            logger.warning(
                "OpenRouterClient initialized without OPENROUTER_API_KEY. Generation calls will fail if network is invoked."
            )

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "Conversational Analytics Agent",
        }

    def _build_payload(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        return {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", 1500),
        }

    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Synchronously execute chat completion call via OpenRouter."""
        if not self.api_key:
            raise LLMError("Cannot call OpenRouter: OPENROUTER_API_KEY is not configured.")

        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers()
        payload = self._build_payload(messages, **kwargs)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenRouter HTTP error: %s - %s", exc.response.status_code, exc.response.text
            )
            raise LLMError(
                f"OpenRouter API error ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except Exception as exc:
            logger.error("OpenRouter generation failed: %s", exc)
            raise LLMError(f"OpenRouter client error: {str(exc)}") from exc

    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Asynchronously execute chat completion call via OpenRouter."""
        if not self.api_key:
            raise LLMError("Cannot call OpenRouter: OPENROUTER_API_KEY is not configured.")

        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers()
        payload = self._build_payload(messages, **kwargs)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenRouter async HTTP error: %s - %s", exc.response.status_code, exc.response.text
            )
            raise LLMError(
                f"OpenRouter API error ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except Exception as exc:
            logger.error("OpenRouter async generation failed: %s", exc)
            raise LLMError(f"OpenRouter client error: {str(exc)}") from exc


class MockLLMClient(BaseLLMClient):
    """Deterministic Mock LLM client for testing and offline execution."""

    def __init__(
        self,
        responses: list[str] | None = None,
        default_response: str | None = None,
    ) -> None:
        """Initialize mock client with predefined response queue or rule-based fallback.

        Args:
            responses: Sequence of response strings to yield in order on consecutive calls.
            default_response: Fallback response if queue is exhausted.
        """
        self._responses: list[str] = list(responses) if responses else []
        self._default_response = default_response
        self.call_history: list[list[dict[str, str]]] = []

    def _determine_response(self, messages: list[dict[str, str]]) -> str:
        self.call_history.append(messages)

        # Pop from pre-configured sequence if available
        if self._responses:
            return self._responses.pop(0)

        if self._default_response:
            return self._default_response

        # Rule-based response heuristic matching context
        user_msg = messages[-1]["content"].lower() if messages else ""

        # Check if this is a repair prompt
        if "failed security and ast guardrail checks" in user_msg:
            # Self-repair response correcting common issues
            return """```sql
SELECT
    o.order_id,
    o.order_status
FROM business.orders o
WHERE o.order_status = 'delivered';
```"""

        if "FAILED while executing against PostgreSQL" in user_msg:
            # Execution self-repair: return a recalled-order aggregate on verified
            # semantic-layer columns (safe fallback for offline/mock runs).
            return """```sql
SELECT
    o.order_status,
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
GROUP BY o.order_status
ORDER BY order_count DESC;
```"""

        # Chasm Trap CTE query
        if "tiền thanh toán" in user_msg or "unsafe flat join detected" in user_msg:
            return """```sql
WITH item_aggs AS (
    SELECT
        oi.order_id,
        oi.seller_id,
        SUM(oi.price) AS total_item_revenue
    FROM business.order_items oi
    GROUP BY oi.order_id, oi.seller_id
),
payment_aggs AS (
    SELECT
        p.order_id,
        SUM(p.payment_value) AS total_order_payment
    FROM business.payments p
    GROUP BY p.order_id
)
SELECT
    ia.seller_id,
    SUM(ia.total_item_revenue) AS total_revenue,
    SUM(pa.total_order_payment) AS total_payment_value
FROM item_aggs ia
JOIN business.orders o ON ia.order_id = o.order_id
LEFT JOIN payment_aggs pa ON o.order_id = pa.order_id
WHERE o.order_status = 'delivered'
GROUP BY ia.seller_id;
```"""

        # Orders in 2018
        if "2018" in user_msg and ("đơn hàng" in user_msg or "orders" in user_msg):
            return """```sql
SELECT
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
WHERE EXTRACT(YEAR FROM o.order_purchase_timestamp) = 2018;
```"""

        # Freight by state
        if "phí vận chuyển" in user_msg or "freight" in user_msg:
            return """```sql
SELECT
    c.customer_state,
    SUM(oi.freight_value) AS total_freight
FROM business.order_items oi
JOIN business.orders o ON oi.order_id = o.order_id
JOIN business.customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state
ORDER BY total_freight DESC;
```"""

        # Revenue by category (default fallback)
        return """```sql
SELECT
    p.product_category_name AS product_category,
    SUM(oi.price) AS total_revenue
FROM business.order_items oi
JOIN business.orders o ON oi.order_id = o.order_id
JOIN business.products p ON oi.product_id = p.product_id
WHERE o.order_status = 'delivered'
GROUP BY p.product_category_name
ORDER BY total_revenue DESC;
```"""

    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self._determine_response(messages)

    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self._determine_response(messages)


def create_llm_client() -> BaseLLMClient:
    """Factory selecting the LLM client from settings.LLM_PROVIDER.

    Supported providers:
        - 'openrouter': OpenRouter gateway using OPENROUTER_API_KEY.
        - 'groq': Groq OpenAI-compatible endpoint using GROQ_API_KEY.
        - 'openai': OpenAI-compatible endpoint using OPENAI_API_KEY.
        - 'mock': deterministic MockLLMClient for tests and offline runs.

    Returns:
        Configured BaseLLMClient instance.
    """
    settings = get_settings()
    provider = settings.LLM_PROVIDER.lower()

    if provider == "groq":
        return OpenRouterClient(
            api_key=settings.GROQ_API_KEY,
            model=settings.LLM_MODEL,
            base_url=settings.GROQ_BASE_URL,
            temperature=settings.LLM_TEMPERATURE,
        )
    if provider == "mock":
        return MockLLMClient()
    if provider == "openai":
        return OpenRouterClient(
            api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_MODEL,
            base_url=settings.LLM_BASE_URL,
            temperature=settings.LLM_TEMPERATURE,
        )
    return OpenRouterClient()
