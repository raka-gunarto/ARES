from __future__ import annotations

import asyncio
import httpx

from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    """Raised when LLM communication fails after retries."""

    pass


class LLMClient:
    """OpenAI-compatible LLM client with retry logic."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        """Initialize LLM client.

        Args:
            base_url: Base URL for the LLM API (e.g., https://api.openai.com/v1)
            api_key: API key for authentication
            model: Model name (e.g., gpt-4)
            timeout_s: Request timeout in seconds (default 60)
            max_retries: Maximum number of retries on failure (default 2)
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
    ) -> dict:
        """Send a chat message and get a response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            tools: Optional list of tool definitions in OpenAI format
            temperature: Sampling temperature (0.0-1.0, default 0.7)

        Returns:
            Raw OpenAI response message dict with 'content' and/or 'tool_calls'

        Raises:
            LLMError: If the request fails after retries or on non-retryable errors
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Build request body
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        # Retry loop: attempt up to max_retries + 1 times
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                log.debug(f"LLM chat attempt {attempt + 1}/{self.max_retries + 1}")
                response = await self._client.post(url, json=body, headers=headers)

                # Retry on 5xx errors
                if 500 <= response.status_code <= 599:
                    last_error = (
                        f"HTTP {response.status_code}: {response.text}"
                    )
                    if attempt < self.max_retries:
                        log.debug(
                            f"Retryable server error {response.status_code}, "
                            f"waiting 2s before retry"
                        )
                        await asyncio.sleep(2)
                        continue
                    # Exhausted retries on 5xx
                    raise LLMError(
                        f"Server error after {self.max_retries + 1} attempts: "
                        f"{last_error}"
                    )

                # Non-retryable client errors (4xx)
                if 400 <= response.status_code < 500:
                    raise LLMError(
                        f"Client error (no retry): HTTP {response.status_code}: "
                        f"{response.text}"
                    )

                # Success case (2xx)
                if 200 <= response.status_code < 300:
                    data = response.json()
                    message = data["choices"][0]["message"]
                    log.debug(f"LLM response: {message}")
                    return message

                # Unexpected status code
                last_error = f"HTTP {response.status_code}: {response.text}"
                if attempt < self.max_retries:
                    log.debug(f"Unexpected status {response.status_code}, retrying")
                    await asyncio.sleep(2)
                    continue
                raise LLMError(f"Unexpected response after retries: {last_error}")

            except httpx.HTTPError as e:
                # Network/timeout errors are retryable
                last_error = str(e)
                if attempt < self.max_retries:
                    log.debug(f"Network error: {e}, waiting 2s before retry")
                    await asyncio.sleep(2)
                    continue
                # Exhausted retries on network error
                raise LLMError(
                    f"Network error after {self.max_retries + 1} attempts: {e}"
                ) from e

        # Should not reach here, but ensure we raise if all retries exhausted
        raise LLMError(f"LLM request failed after {self.max_retries + 1} attempts")

    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()
