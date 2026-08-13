# DocChat — Capabilities & Architecture

**What it is:** Local RAG chat over **any folder or file you choose**. A small
embedding **server that fused-render spins up on demand** keeps a real model and
your indexes **warm in memory**; answers come from **Fused AI** (the local Claude
CLI) or extractive passages. No Ollama, no cloud storage of your files.

## What it can do
- **Pick any folder OR a single file** via a **Browse…** dialog — clickable
  breadcrumb, **Recent** folders, a **paste-a-path** field (Windows "Copy as path"
  works: quotes stripped, backslashes handled) — or the sidebar path box. Click a
  file to use just that file.
- Index **all text-readable files, recursively** (`.md/.txt/.html/.py/.js/.ts/.json/
  .yaml/.csv/.sql/…`; HTML is tag-stripped). Binaries, PDFs, VCS/build dirs
  (`.git`, `node_modules`, `__pycache__`, …) and **hidden files** are skipped — and
  the sidebar **shows what it skipped**.
- **Good local embeddings, GPU-accelerated when available.** Default model
  **`Qwen/Qwen3-Embedding-0.6B`** (Apache-2.0, 1024-dim). Pick `bge-base`,
  `bge-small`, or `bge-m3` from the **header dropdown** (or `$RAG_MODEL`). Loaded
  once by the server via **sentence-transformers**, which auto-uses **CUDA/MPS if
  present, else CPU**. First use of an un-downloaded model shows a **Download**
  button (permission gate) and pulls it into the cache.
- **Incremental indexing.** Re-indexing re-embeds only **added/changed files**
  (per-file mtime) and drops removed ones — editing one file in a big folder does
  not re-embed the whole tree. Indexing runs **server-side, free of the 60 s call
  limit**, with a live progress rule; interrupted builds **resume**.
- **Fast follow-up questions.** Because the model + indexes stay warm, a question
  is just embed-query + vector-search — **no re-walk, re-chunk, or re-embed**.
- **Answers:** **Fused AI** (local Claude CLI) writes a grounded, cited answer
  (markdown-rendered), or **Passages** shows the top matching excerpts with scores.
- **Indexed-file list** with per-file chunk counts + filter. Click a file → a
  **preview pane** renders it with **fused-render's own viewers** (JSON tree,
  tables, images, PDF, code) via the `/embed` route, plus a **Text** view that
  shows the raw indexed text and highlights the cited chunk.
- **Movable cache.** Change where indexes are saved from the sidebar's **Saved to**
  control; existing indexes are **moved, not rebuilt**.
- **"Warm workstation" UI** — sharp (no rounded cards), mono for the machinery and
  serif for answers; a three-pane layout (source/index/**file list** rail | ledger
  chat | file-preview), light/dark toggle, and a ● server/model/device status.

## What it cannot do
- **First run downloads the model** (~1.2 GB for Qwen3-0.6B) and needs the internet
  once; after that it's fully local and offline.
- On a CPU-only machine it works but **indexing is slower** than on a GPU — by
  design, quality first. Retrieval stays snappy once warm.
- No PDFs/Office as *source* (that's PaperReader — though the preview pane can
  *render* them). Fused AI answers need the `claude` CLI; without it, DocChat falls
  back to Passages automatically.

## Full-system RAG ("all my files")?
Yes for a folder — point the picker at any root and it walks it (capped at 5,000
files/build for safety). Limits:
- **Embedding (bottleneck):** on CPU, a 0.6B model does tens of chunks/sec; a GPU is
  10–50× faster — but embedding happens **once** per file (incremental thereafter).
- **Retrieval:** HNSW is sub-linear — comfortable into the **millions** of chunks.
- **Verdict:** best of the four apps for scale; the sweet spot is a project / notes / repo.

## Architecture
`serve.py` (one `runPython` call) launches **`ragserver.py`** detached + windowless.
The page then talks to it directly over `fetch` (CORS-enabled, fixed port `8271`):
`chosen folder → walk text files → chunk (~700 chars) → embed (warm
sentence-transformers, GPU/CPU) → per-(folder, model) DuckDB + HNSW(cosine)` →
query: `embed question → HNSW top-k → [Fused AI | Passages]`. Indexes and the model
live in the server's memory across questions, and on disk under
**`~/.fused-render/cache/`** (`docchat/` for indexes, `models/` for weights).

## Backend files (where it lives)
| File | Role |
|---|---|
| `ragserver.py` | The warm server: loads the model once, builds/holds per-folder **DuckDB+HNSW** indexes incrementally, serves `/health /status /browse /index /search /files /file /cached /movecache`. Core fns (`build_index`/`search_index`/`embed_*`) are unit-tested. |
| `serve.py` | `main()` → ensure the server is up (spawn **detached, `CREATE_NO_WINDOW`** on Windows — no console popup; `start_new_session` on POSIX), return its port. The only `runPython` the app makes. |
| `rag_common.py` | Folder walking + file-type filter, chunking, HTML→text, per-folder DuckDB/VSS helpers, cache paths. |
| `docchat.html` | Chat UI + rail (source / index / file list) + folder modal + file-preview pane; talks to the server via `fetch`, answers via `fused.ai`. |
| `tests/test_rag.py` | pytest: chunking, **incremental reuse (only changed files re-embed)**, retrieval, cache relocation, file list, hidden-file skipping. |
| `pyproject.toml` | Deps: `sentence-transformers`, `duckdb`, `numpy` (torch comes with ST). |
| `~/.fused-render/cache/` | `docchat/` (per-(folder, model) DuckDB indexes) + `models/` (weights); `.ragserver.json` is the server sidecar (git-ignored). |

**Model:** `Qwen/Qwen3-Embedding-0.6B` (1024-d) by default, any HF/sentence-transformers
model via the picker or `RAG_MODEL`. **Answers:** Fused AI (`claude-haiku`) or extractive passages.
**Cross-platform** (Windows/macOS/Linux). **Tests:** 11 (MiniLM) green — `pytest tests/`.
