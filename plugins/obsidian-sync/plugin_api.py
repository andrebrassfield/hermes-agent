"""Obsidian Vault Integration Plugin — search local markdown files.

Exposes:
- ``search_obsidian`` tool — search .md files in a vault directory
- FastAPI router — mounted at ``/api/plugins/obsidian-sync/``
"""



import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models for the API
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query string")
    vault_path: str = Field(default="~/vault", description="Path to the Obsidian vault")
    limit: int = Field(default=5, ge=1, le=50, description="Maximum number of results to return")


class MatchResult(BaseModel):
    path: str = Field(..., description="Relative path within the vault")
    score: float = Field(..., description="Relevance score (higher is better)")
    snippet: str = Field(..., description="Relevant text snippet around the match")


class SearchResponse(BaseModel):
    query: str
    vault_path: str
    results: List[MatchResult]
    total_scanned: int


# Rebuild to resolve forward references in dynamic-import contexts (Pydantic v2)
SearchResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Search logic
# ---------------------------------------------------------------------------

def _expand_vault_path(vault_path: str) -> Path:
    """Expand user home and environment variables in the vault path."""
    expanded = os.path.expanduser(vault_path)
    expanded = os.path.expandvars(expanded)
    return Path(expanded).resolve()


def _tokenize(query: str) -> List[str]:
    """Lower-case, de-duplicate tokens; drop very short tokens."""
    tokens = re.findall(r"[a-zA-Z0-9_\-\']+", query.lower())
    return [t for t in dict.fromkeys(tokens) if len(t) >= 2]


def _score_file(
    file_path: Path,
    rel_path: str,
    tokens: List[str],
    content: str,
) -> Optional[float]:
    """Compute a relevance score for a single file.

    Scoring heuristic:
    - Token match in filename: +10 per token
    - Token match in content: +3 per occurrence
    - Consecutive token matches (phrase-like): +5 bonus per pair
    """
    if not tokens:
        return None

    lower_content = content.lower()
    lower_name = file_path.name.lower()
    score = 0.0

    # Filename matches
    for token in tokens:
        if token in lower_name:
            score += 10.0

    # Content matches
    token_positions: Dict[str, List[int]] = {}
    for token in tokens:
        positions = []
        start = 0
        while True:
            idx = lower_content.find(token, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(token)
        if positions:
            score += 3.0 * len(positions)
            token_positions[token] = positions

    if not token_positions:
        return None

    # Phrase bonus: tokens appearing close together
    if len(tokens) > 1 and all(t in token_positions for t in tokens):
        for i in range(len(tokens) - 1):
            t1, t2 = tokens[i], tokens[i + 1]
            for p1 in token_positions[t1]:
                for p2 in token_positions[t2]:
                    if abs(p1 - p2) <= len(t1) + 20:
                        score += 5.0
                        break

    return score


def _extract_snippet(content: str, tokens: List[str], snippet_len: int = 240) -> str:
    """Extract a snippet around the best token match."""
    lower = content.lower()
    best_idx = -1
    best_score = -1

    for token in tokens:
        idx = lower.find(token)
        if idx == -1:
            continue
        # Prefer snippets that contain more distinct tokens
        window = lower[max(0, idx - 60): idx + snippet_len]
        score = sum(1 for t in tokens if t in window)
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx == -1:
        return content[:snippet_len].strip()

    start = max(0, best_idx - snippet_len // 4)
    end = min(len(content), start + snippet_len)
    # Adjust start if end was clamped
    if end == len(content):
        start = max(0, end - snippet_len)

    snippet = content[start:end]
    # Clean up partial words at edges
    if start > 0 and snippet[0].isalnum():
        snippet = "…" + snippet[snippet.find(" ") + 1:]
    if end < len(content) and snippet[-1].isalnum():
        last_space = snippet.rfind(" ")
        if last_space != -1:
            snippet = snippet[:last_space] + "…"
    return snippet.strip()


def _search_vault(vault_path: Path, query: str, limit: int) -> SearchResponse:
    """Core search implementation."""
    tokens = _tokenize(query)
    results: List[Dict[str, Any]] = []
    total_scanned = 0

    if not vault_path.exists():
        logger.warning("Vault path does not exist: %s", vault_path)
        return SearchResponse(
            query=query,
            vault_path=str(vault_path),
            results=[],
            total_scanned=0,
        )

    md_files = list(vault_path.rglob("*.md"))
    for file_path in md_files:
        total_scanned += 1
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping unreadable file %s: %s", file_path, exc)
            continue

        try:
            rel_path = str(file_path.relative_to(vault_path))
        except ValueError:
            rel_path = str(file_path)

        score = _score_file(file_path, rel_path, tokens, content)
        if score is None:
            continue

        snippet = _extract_snippet(content, tokens)
        results.append({
            "path": rel_path,
            "score": score,
            "snippet": snippet,
        })

    # Sort by descending score, then by path for stability
    results.sort(key=lambda r: (-r["score"], r["path"]))
    top = results[:limit]

    return SearchResponse(
        query=query,
        vault_path=str(vault_path),
        results=[MatchResult(**m) for m in top],
        total_scanned=total_scanned,
    )


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchResponse)
def api_search_obsidian(request: SearchRequest) -> SearchResponse:
    """Search an Obsidian vault for markdown notes matching a query."""
    vault = _expand_vault_path(request.vault_path)
    return _search_vault(vault, request.query, request.limit)


@router.get("/search", response_model=SearchResponse)
def api_search_obsidian_get(
    query: str = Query(..., min_length=1),
    vault_path: str = Query(default="~/vault"),
    limit: int = Query(default=5, ge=1, le=50),
) -> SearchResponse:
    """GET variant for lightweight vault search."""
    vault = _expand_vault_path(vault_path)
    return _search_vault(vault, query, limit)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

_SEARCH_OBSIDIAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_obsidian",
        "description": (
            "Search a local Obsidian vault for markdown notes. "
            "Returns top matching files sorted by relevance, with file path and a text snippet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string to match against note titles and content",
                },
                "vault_path": {
                    "type": "string",
                    "description": "Absolute or ~-prefixed path to the Obsidian vault directory (default: ~/vault)",
                    "default": "~/vault",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-50)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


def _handle_search_obsidian(
    query: str,
    vault_path: str = "~/vault",
    limit: int = 5,
    **_: Any,
) -> str:
    """Tool handler for search_obsidian."""
    vault = _expand_vault_path(vault_path)
    response = _search_vault(vault, query, limit)

    if not response.results:
        return (
            f"No matches found for '{query}' in {response.vault_path} "
            f"({response.total_scanned} files scanned)."
        )

    lines = [
        f"Obsidian search: '{query}' in {response.vault_path}",
        f"Scanned {response.total_scanned} markdown files, {len(response.results)} match(es):",
        "",
    ]
    for i, match in enumerate(response.results, 1):
        lines.append(f"{i}. {match.path} (score: {match.score:.1f})")
        lines.append(f"   {match.snippet}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the Obsidian vault search tool."""
    ctx.register_tool(
        name="search_obsidian",
        toolset="obsidian",
        schema=_SEARCH_OBSIDIAN_SCHEMA,
        handler=_handle_search_obsidian,
        emoji="📝",
    )
