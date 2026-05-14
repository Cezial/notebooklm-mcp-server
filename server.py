import asyncio
import os
import re
import time
import uuid
import logging
from contextvars import ContextVar
from pathlib import Path
from fastmcp import FastMCP
from notebooklm import NotebookLMClient
import notebooklm._chat as _chat_module

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("notebooklm-mcp")

AUTH_PATH = os.getenv("AUTH_STORAGE_PATH", os.path.expanduser("~/.notebooklm/storage_state.json"))

# Per-coroutine state: status code from last failed chat parse (None if success
# or no recognized error pattern). Set by _patched_parse, read by ask_notebook.
_last_parse_status: ContextVar[int | None] = ContextVar("_last_parse_status", default=None)

# Pattern from server-rejected response: [["wrb.fr",null,null,null,null,[N]]]
# where N is a bare RPC status code (e.g. 3 = INVALID_ARGUMENT, observed when
# prompt exceeds Google's input size limit). Lib _raise_if_rate_limited expects
# longer payload shape [N, None, [[UserDisplayableError, ...]]] and silently
# falls through on bare codes, causing empty-answer with no diagnostic.
_BARE_STATUS_PATTERN = re.compile(r'\["wrb\.fr",null,null,null,null,\[(\d+)\]\]')


def _extract_bare_status(response_text: str) -> int | None:
    m = _BARE_STATUS_PATTERN.search(response_text)
    return int(m.group(1)) if m else None


# ==================== Capture patch (upstream bug investigation) ====================
# Monkey-patch _parse_ask_response_with_references to (1) dump raw response_text
# when parser fails (empty answer) for failing-sample collection, and (2) extract
# bare RPC status code into a ContextVar so ask_notebook can short-circuit retry
# on deterministic server-rejected requests (e.g. prompt too long → status 3).
if os.getenv("CAPTURE_DUMPS", "1") == "1":
    _DUMP_DIR = Path(os.getenv("CAPTURE_DUMP_DIR", "/app/dumps"))
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    _original_parse = _chat_module.ChatAPI._parse_ask_response_with_references

    def _patched_parse(self, response_text):
        answer, refs, conv_id = _original_parse(self, response_text)
        success = bool(answer and answer.strip())
        prefix = "ok" if success else "fail"
        fname = _DUMP_DIR / f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.txt"
        try:
            fname.write_text(response_text)
            logger.warning(
                "[CAPTURE] dumped %s len=%d answer_len=%d -> %s",
                prefix, len(response_text), len(answer or ""), fname.name,
            )
        except Exception as e:
            logger.error("[CAPTURE] dump failed: %s", e)
        # Surface bare status code for ask_notebook short-circuit logic.
        if success:
            _last_parse_status.set(None)
        else:
            status = _extract_bare_status(response_text)
            _last_parse_status.set(status)
            if status is not None:
                logger.warning(
                    "[CAPTURE] server-rejected response, bare status_code=%d "
                    "(deterministic — skipping retry)", status,
                )
        return answer, refs, conv_id

    _chat_module.ChatAPI._parse_ask_response_with_references = _patched_parse
    logger.warning("[CAPTURE] monkey-patch active; dumps -> %s", _DUMP_DIR)

mcp = FastMCP("NotebookLM")


async def get_client() -> NotebookLMClient:
    return await NotebookLMClient.from_storage(path=AUTH_PATH)


# ==================== Notebook Management ====================

@mcp.tool
async def list_notebooks() -> list[dict]:
    """List all NotebookLM notebooks with their IDs and titles."""
    async with await get_client() as client:
        notebooks = await client.notebooks.list()
        return [{"id": nb.id, "title": nb.title} for nb in notebooks]


@mcp.tool
async def create_notebook(title: str) -> dict:
    """Create a new NotebookLM notebook.

    Args:
        title: Title for the new notebook
    """
    async with await get_client() as client:
        nb = await client.notebooks.create(title)
        return {"id": nb.id, "title": nb.title}


@mcp.tool
async def rename_notebook(notebook_id: str, new_title: str) -> dict:
    """Rename an existing notebook.

    Args:
        notebook_id: ID of the notebook to rename
        new_title: New title for the notebook
    """
    async with await get_client() as client:
        nb = await client.notebooks.rename(notebook_id, new_title)
        return {"id": nb.id, "title": nb.title}


@mcp.tool
async def delete_notebook(notebook_id: str) -> bool:
    """Delete a notebook permanently.

    Args:
        notebook_id: ID of the notebook to delete
    """
    async with await get_client() as client:
        return await client.notebooks.delete(notebook_id)


@mcp.tool
async def get_notebook_summary(notebook_id: str) -> str:
    """Get an AI-generated summary of a notebook's contents.

    Args:
        notebook_id: ID of the notebook
    """
    async with await get_client() as client:
        return await client.notebooks.get_summary(notebook_id)


# ==================== Source Management ====================

@mcp.tool
async def list_sources(notebook_id: str) -> list[dict]:
    """List all sources in a notebook.

    Args:
        notebook_id: ID of the notebook
    """
    async with await get_client() as client:
        sources = await client.sources.list(notebook_id)
        return [{"id": s.id, "title": s.title} for s in sources]


@mcp.tool
async def add_source_url(notebook_id: str, url: str) -> dict:
    """Add a web URL or YouTube link as a source to a notebook.

    Args:
        notebook_id: ID of the notebook
        url: Web URL or YouTube URL to add
    """
    async with await get_client() as client:
        source = await client.sources.add_url(notebook_id, url)
        return {"id": source.id, "title": source.title}


@mcp.tool
async def add_source_text(notebook_id: str, title: str, content: str) -> dict:
    """Add text/markdown content as a source to a notebook.

    Args:
        notebook_id: ID of the notebook
        title: Title for the source
        content: Text or markdown content
    """
    async with await get_client() as client:
        source = await client.sources.add_text(notebook_id, title, content)
        return {"id": source.id, "title": source.title}


@mcp.tool
async def add_source_file(
    notebook_id: str,
    file_path: str,
    wait: bool = True,
    wait_timeout: float = 120.0,
) -> dict:
    """Add a local file as a source to a notebook (PDF, TXT, MD, DOCX, EPUB).

    Args:
        notebook_id: ID of the notebook
        file_path: Absolute path to the file INSIDE the container. The default
            compose only mounts ~/.notebooklm:/auth — to upload host files,
            mount the host directory in docker-compose.yml (e.g.
            "~/Documents:/host-docs:ro") and pass paths like /host-docs/foo.pdf.
        wait: If True, wait for the source to finish processing before returning.
        wait_timeout: Maximum seconds to wait when wait=True (default 120).
    """
    async with await get_client() as client:
        source = await client.sources.add_file(
            notebook_id, file_path, wait=wait, wait_timeout=wait_timeout
        )
        return {"id": source.id, "title": source.title}


@mcp.tool
async def wait_for_source_ready(
    notebook_id: str,
    source_id: str,
    timeout: float = 120.0,
) -> dict:
    """Wait until a source has finished processing in NotebookLM.

    Args:
        notebook_id: ID of the notebook
        source_id: ID of the source returned by add_source_*
        timeout: Maximum seconds to wait (default 120)
    """
    async with await get_client() as client:
        source = await client.sources.wait_until_ready(
            notebook_id, source_id, timeout=timeout
        )
        return {"id": source.id, "title": source.title}


@mcp.tool
async def delete_source(notebook_id: str, source_id: str) -> bool:
    """Delete a source from a notebook.

    Args:
        notebook_id: ID of the notebook
        source_id: ID of the source to delete
    """
    async with await get_client() as client:
        return await client.sources.delete(notebook_id, source_id)


# ==================== Chat & Research ====================

@mcp.tool
async def ask_notebook(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None = None,
    conversation_id: str | None = None,
    max_retries: int = 3,
) -> dict:
    """Ask a question grounded in the notebook's sources. Returns answer with citations.

    Persists the Q&A turn into the notebook's server-side conversation so it
    is later retrievable via get_conversation_history. If conversation_id is
    not given, the notebook's current server-managed conversation_id is
    fetched and used (required for persistence — passing None makes the lib
    generate a client UUID that the server discards).

    Empty-answer handling distinguishes two failure modes:

    1. **Deterministic server rejection** — Google returns a bare RPC status
       code (e.g. `[3]` INVALID_ARGUMENT, observed when prompt exceeds Google's
       input size limit). Returns immediately with `status_code` + `hint`; does
       NOT retry because retry won't change the outcome.

    2. **Transient parser miss** — empty answer with no recognizable status
       code (Google response stream lacked expected text chunks). Auto-retries
       up to `max_retries` with exponential backoff 2s → 5s → 10s, reusing the
       same conversation_id to preserve server-side context.

    Response always includes `attempts` for transparency. On terminal failure,
    `error` + `hint` fields guide callers (shorten prompt, wait & retry, or
    fall back to NotebookLM web UI).

    Args:
        notebook_id: ID of the notebook
        question: Question to ask
        source_ids: Optional list of specific source IDs to query against
        conversation_id: Optional. If None, uses the notebook's most recent
            server-managed conversation_id so the turn persists into history.
        max_retries: Maximum attempts on transient empty answer (default 3).
    """
    backoff = [2, 5, 10]
    async with await get_client() as client:
        conv_id = conversation_id or await client.chat.get_conversation_id(notebook_id)
        last_result = None
        for attempt in range(max_retries):
            last_result = await client.chat.ask(
                notebook_id, question, source_ids=source_ids, conversation_id=conv_id
            )
            if last_result.answer and last_result.answer.strip():
                return {
                    "answer": last_result.answer,
                    "conversation_id": last_result.conversation_id,
                    "turn_number": last_result.turn_number,
                    "attempts": attempt + 1,
                }
            # Empty answer — distinguish deterministic server rejection vs transient parser miss.
            status = _last_parse_status.get()
            if status is not None:
                logger.warning(
                    "ask_notebook deterministic fail (status=%d) for notebook %s — skipping retry",
                    status, notebook_id,
                )
                return {
                    "answer": "",
                    "conversation_id": last_result.conversation_id,
                    "turn_number": 0,
                    "attempts": attempt + 1,
                    "error": f"server_rejected_status_{status}",
                    "status_code": status,
                    "hint": (
                        f"Server rejected the request with bare RPC status code {status} "
                        "(likely INVALID_ARGUMENT — prompt exceeded Google's input size limit, "
                        "or contained content that triggered a server-side filter). Retry "
                        "won't help. Shorten the prompt, split into multiple smaller asks, "
                        "or paste into NotebookLM web UI for very large queries."
                    ),
                }
            logger.warning(
                "ask_notebook transient empty answer attempt %d/%d for notebook %s",
                attempt + 1, max_retries, notebook_id,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
        return {
            "answer": "",
            "conversation_id": last_result.conversation_id if last_result else None,
            "turn_number": 0,
            "attempts": max_retries,
            "error": "empty_answer_after_retries",
            "hint": (
                "Lib parser failed to extract answer from Google response stream after "
                f"{max_retries} attempts (transient — no server status code surfaced). "
                "Wait ~30s and retry, or paste prompt into NotebookLM web UI as fallback."
            ),
        }


@mcp.tool
async def get_conversation_history(
    notebook_id: str,
    conversation_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Get Q&A history for a notebook conversation, oldest-first.

    NotebookLM stores ONE active conversation per notebook server-side; new
    asks append turns to that conversation rather than creating new ones.
    Default returns the most recent conversation. Pass conversation_id to
    read a specific past conversation if you saved its ID earlier.

    Args:
        notebook_id: ID of the notebook
        conversation_id: Optional. If None, uses the most recent conversation.
        limit: Maximum number of Q&A turns to retrieve (default 100)

    Returns:
        List of {"question", "answer"} pairs, oldest-first. Empty list if no
        conversations exist.
    """
    async with await get_client() as client:
        history = await client.chat.get_history(
            notebook_id, limit=limit, conversation_id=conversation_id
        )
        return [{"question": q, "answer": a} for q, a in history]


@mcp.tool
async def web_research(notebook_id: str, query: str, mode: str = "fast") -> dict:
    """Use NotebookLM's web research agent to search and synthesize information.

    Args:
        notebook_id: ID of the notebook to add research results to
        query: Research query
        mode: Research mode - "fast" for quick results, "deep" for thorough research
    """
    async with await get_client() as client:
        result = await client.research.start(notebook_id, query, source="web", mode=mode)
        return result


# ==================== Content Generation ====================

@mcp.tool
async def generate_audio_overview(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate an audio podcast overview of the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the audio (e.g. "make it fun", "focus on chapter 3")
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_audio(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_video_overview(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate a video overview of the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the video
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_video(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_slide_deck(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate a slide deck from the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the slides
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_slide_deck(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_mind_map(
    notebook_id: str,
    instructions: str | None = None,
) -> dict:
    """Generate a mind map from the notebook's content. Returns structured JSON.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the mind map
    """
    async with await get_client() as client:
        return await client.artifacts.generate_mind_map(
            notebook_id, instructions=instructions
        )


@mcp.tool
async def generate_infographic(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate an infographic from the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the infographic
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_infographic(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_quiz(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate a quiz from the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions (e.g. "focus on chapter 2", "make it hard")
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_quiz(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_flashcards(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate flashcards from the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for the flashcards
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_flashcards(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_report(
    notebook_id: str,
    title: str | None = None,
    description: str | None = None,
    language: str = "en",
) -> dict:
    """Generate a report (briefing doc, study guide, or blog post) from the notebook.

    Args:
        notebook_id: ID of the notebook
        title: Optional title for the report
        description: Optional description/instructions for the report
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_report(
            notebook_id, title=title, description=description, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


@mcp.tool
async def generate_data_table(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> dict:
    """Generate a structured data table from the notebook's content.

    Args:
        notebook_id: ID of the notebook
        instructions: Optional instructions for what data to extract
        language: Language code (default: "en")
    """
    async with await get_client() as client:
        status = await client.artifacts.generate_data_table(
            notebook_id, instructions=instructions, language=language
        )
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        return {"status": "completed", "task_id": status.task_id}


# ==================== Sharing ====================

@mcp.tool
async def share_notebook(notebook_id: str, public: bool = True) -> dict:
    """Share a notebook by creating a public or private link.

    Args:
        notebook_id: ID of the notebook
        public: If True, create a public link. If False, make private.
    """
    async with await get_client() as client:
        status = await client.sharing.set_public(notebook_id, public)
        return {"public": public, "status": str(status)}


# ==================== Health Check ====================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "service": "notebooklm-mcp"})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "10007"))
    mcp.run(transport="streamable-http", host=host, port=port)
