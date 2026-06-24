"""Thin async client for the ``retrieval`` service — our single model gateway.

document_intelligence holds NO Stellar/COIN/VDI credentials. All embeddings, LLM completions,
and reranking go through the retrieval service's (to-be-added) endpoints — see
``reports/retrieval-api-requirements.md``:

    POST /api/embed           -> vectors
    POST /api/llm/complete    -> {text, model, usage}
    POST /api/rerank          -> ranked candidates
    GET  /api/models          -> {embedding_dim, tasks, ...}

When ``DI_RETRIEVAL_STUB=true`` (or no base URL), a deterministic in-process fake is used so the
pipeline is exercisable without the live service (zero-vectors of the configured dim + echo
completions). The interface is identical, so swapping is transparent.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

from di.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RetrievalError(RuntimeError):
    pass


class RetrievalClient:
    """Live HTTP client against the retrieval service."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        headers = {}
        if self._s.retrieval_api_key:
            headers["X-API-KEY"] = self._s.retrieval_api_key
        self._http = httpx.AsyncClient(
            base_url=self._s.retrieval_base_url.rstrip("/"),
            headers=headers,
            timeout=self._s.retrieval_timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:  # noqa: BLE001 - wrap into a domain error
            raise RetrievalError(f"retrieval {path} failed: {e}") from e

    async def embed(self, texts: list[str], task: str = "embedding") -> list[list[float]]:
        if not texts:
            return []
        data = await self._post("/api/embed", {"texts": texts, "task": task})
        return data["vectors"]

    async def llm_complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "final_gen",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str = "text",
    ) -> tuple[str, dict[str, int]]:
        data = await self._post(
            "/api/llm/complete",
            {
                "messages": messages,
                "task": task,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            },
        )
        return data["text"], data.get("usage", {})

    async def rerank(
        self, query: str, candidates: list[dict[str, str]], top_k: int = 20
    ) -> list[dict[str, Any]]:
        data = await self._post(
            "/api/rerank", {"query": query, "candidates": candidates, "top_k": top_k}
        )
        return data["ranked"]

    async def models(self) -> dict[str, Any]:
        try:
            resp = await self._http.get("/api/models")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:  # noqa: BLE001
            raise RetrievalError(f"retrieval /api/models failed: {e}") from e


class StubRetrievalClient:
    """Deterministic offline fake. Embeddings are seeded hashes (stable per text); completions
    echo a structured stub. Lets the whole pipeline run with no external service."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._dim = (settings or get_settings()).embedding_dim_default

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # deterministic pseudo-vector in [-1, 1], repeated to fill the dim
        base = [(b / 127.5) - 1.0 for b in h]
        out = (base * ((self._dim // len(base)) + 1))[: self._dim]
        return out

    async def embed(self, texts: list[str], task: str = "embedding") -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def llm_complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "final_gen",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str = "text",
    ) -> tuple[str, dict[str, int]]:
        last = messages[-1]["content"] if messages else ""
        if response_format == "json":
            return '{"stub": true}', {"prompt": 0, "completion": 0, "total": 0}
        return f"[stub:{task}] {last[:120]}", {"prompt": 0, "completion": 0, "total": 0}

    async def rerank(
        self, query: str, candidates: list[dict[str, str]], top_k: int = 20
    ) -> list[dict[str, Any]]:
        return [{"id": c["id"], "score": 1.0 - i * 0.01} for i, c in enumerate(candidates[:top_k])]

    async def models(self) -> dict[str, Any]:
        return {"provider": "stub", "embedding_dim": self._dim, "tasks": {}}


def get_retrieval_client(settings: Settings | None = None) -> RetrievalClient | StubRetrievalClient:
    settings = settings or get_settings()
    if settings.di_retrieval_stub or not settings.retrieval_base_url:
        logger.info("using StubRetrievalClient (offline model gateway)")
        return StubRetrievalClient(settings)
    return RetrievalClient(settings)
