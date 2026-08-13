# DocChat — local RAG chat over your documents

A conversational RAG app built as a **fused-render view**. Point it at a folder
(or a single file) and ask questions grounded in what's inside. Everything runs
**on your machine** — embeddings are computed locally with
[sentence-transformers](https://www.sbert.net/); answers are written by the local
`claude` CLI (Fused AI) or shown as the raw retrieved passages. **No Ollama, no
cloud, no API keys.**

![DocChat](../../assets/docchat.png)

- **Embeddings:** a sentence-transformers model, loaded **once** by a warm
  in-process server and kept in memory. Default `Qwen/Qwen3-Embedding-0.6B`;
  switchable to `BAAI/bge-base-en-v1.5`, `BAAI/bge-small-en-v1.5`, or
  `BAAI/bge-m3` from the header (or `$RAG_MODEL`). Auto-uses CUDA/MPS if present,
  else CPU.
- **Vector store + retrieval:** **DuckDB** with the **VSS** extension — a
  `FLOAT[N]` embedding column and a **HNSW** index (`metric = 'cosine'`), queried
  with `array_cosine_distance` / `array_cosine_similarity`.
- **Generation:** the local **Claude CLI** via `fused.ai` (Fused AI), grounded in
  the retrieved chunks and cited. If the CLI isn't available, DocChat falls back
  to **Passages** (extractive) automatically.

## Open it

The fused-render desktop server must be running (default port `1777`). Open the
app from the explorer, or navigate directly (swap in your absolute path):

```
http://127.0.0.1:1777/explorer/view/<abs-path-to>/docchat/docchat.html
```

No setup step, no model download prompt to run by hand — the first time you pick
a model that isn't on disk yet, the header shows a **Download** button and pulls
it into the fused-render cache.

## How a turn works

1. `docchat.html` calls `serve.py` once (via `fused.runPython`) to make sure the
   embedding server (`ragserver.py`) is up. The server binds a fixed local port
   (`8271`) and stays warm; the page talks to it directly over `fetch`.
2. Choosing a source **indexes** it server-side (walk → chunk → embed → DuckDB +
   HNSW). Indexing runs **outside the 60 s `runPython` limit** and reports live
   progress via `/status`. It's **incremental**: re-indexing only re-embeds
   added/changed files and drops removed ones.
3. A question is `embed-query → HNSW top-k` — never a re-walk or re-embed, so
   follow-ups are fast.
4. **Fused AI** writes a cited answer from the top chunks (markdown-rendered),
   with a collapsible **Sources** list. Click a source to open that file in the
   preview pane, scrolled to the cited chunk.

## Where things are stored

Everything DocChat downloads or builds lives under **`~/.fused-render/cache/`**:

- **Index cache:** `~/.fused-render/cache/docchat/` — one DuckDB file per
  `(folder, model)`. Movable from the sidebar's **Saved to** control (existing
  indexes are carried over, not rebuilt) or via `$RAG_CACHE_DIR`.
- **Model weights:** `~/.fused-render/cache/models/` (`$HF_HOME`).

## Features

- **Pick a folder or a single file** — a *Browse…* dialog (breadcrumb, Recent,
  paste-a-path) or the sidebar path box. Windows "Copy as path" works (quotes
  stripped, backslashes handled).
- **Indexes text-readable files recursively** (`.md/.txt/.html/.py/.js/.ts/.json/
  .yaml/.csv/.sql/…`; HTML is tag-stripped). VCS/build/hidden folders (`.git`,
  `node_modules`, `__pycache__`, …) and hidden files are **skipped by default** —
  and the sidebar shows what it skipped.
- **Indexed-file list** with per-file chunk counts and a filter. Click a file →
  the **preview pane** renders it with fused-render's own viewers (JSON tree,
  tables, images, PDF, code) via the `/embed` route, with a **Text** toggle that
  shows the raw indexed text and highlights the cited chunk.
- **Fused AI / Passages** toggle, **model picker** with first-run download gate,
  light/dark theme.

## Try these questions

The bundled `docs/` folder is a small, cross-referenced café operations handbook:

- *How do I dial in an espresso shot — what dose, yield and grind?* → `espresso.md`
  (+ `beans.md` on freshness)
- *How should I store coffee beans, and how long do they stay fresh?* → `beans.md`
- *What are the steps to close the café at night?* → `closing.md`, `cleaning.md`,
  `cash-handling.md`
- *How often should I backflush the machine, and with what?* → `cleaning.md` +
  `equipment.md`

## Files

| Path | Role |
|---|---|
| `docchat.html` | The chat UI: sidebar (source / index / file list), ledger transcript, file-preview pane. Talks to the server over `fetch`; answers via `fused.ai`. |
| `serve.py` | Launcher: ensure `ragserver.py` is running (detached, windowless on Windows) and report its port. The only `runPython` the page makes. |
| `ragserver.py` | The warm embedding server: loads the model once, builds/holds per-folder DuckDB+HNSW indexes (incrementally), serves `/health /status /browse /index /search /files /file /cached /movecache`. |
| `rag_common.py` | Folder walking + file-type filter, chunking, HTML→text, DuckDB/VSS helpers, cache paths. |
| `tests/test_rag.py` | pytest suite (chunking, incremental reuse, retrieval, cache relocation, file list, hidden-file skipping). |
| `pyproject.toml` | Deps: `sentence-transformers`, `duckdb`, `numpy` (torch comes with sentence-transformers). |
| `docs/` | 8 example markdown docs used as the default source. |

## Run the tests

```
uv run --no-project --with sentence-transformers --with duckdb --with numpy \
    --with pytest pytest tests/ -q
```

They run against a small model (`all-MiniLM-L6-v2`) so they're quick, but exercise
the real path: chunk → embed → DuckDB + HNSW → cosine search, including the
incremental-reuse guard (an unchanged file must not be re-embedded).
