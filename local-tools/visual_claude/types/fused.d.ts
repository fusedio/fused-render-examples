// Ambient types for the fused-render runtime bridge (architecture.md §2, §3a).
// The desktop app injects `window.fused` into every rendered page; it is never
// imported, so we declare it globally here. Typing this is the highest-value
// check in the project: it turns `fused.*` misuse (e.g. params.set with a
// non-string, or a typo'd method) into a compile-time error under tsc --checkJs.

/** File metadata returned by `fused.stat` / `fused.writeFile`. */
interface FusedStat {
  path: string;
  name: string;
  is_dir: boolean;
  size: number;
  mtime: number;
  template: string | null;
}

/** Optimistic-lock options for `fused.writeFile`. */
interface FusedWriteOpts {
  /** Reject with a `conflict` error if the file changed since this mtime. */
  expectedMtime?: number;
}

/** URL-synced view state — string key/values only (they live in the URL). */
interface FusedParams {
  /** Current value for `k`, or undefined. Always a string. */
  get(k: string): string | undefined;
  /** All non-reserved params as a plain object. */
  getAll(): Record<string, string>;
  /** Write `k`=`v` to the URL. `v` MUST be a string — throws otherwise. */
  set(k: string, v: string): void;
  /** Subscribe to param changes; returns an unsubscribe function. */
  onChange(cb: (all: Record<string, string>) => void): () => void;
}

interface Fused {
  /**
   * Run `main(**params)` of the .py at `py` (relative to this page's dir, or
   * absolute) in a fresh subprocess. Resolves with its JSON return value;
   * rejects with an Error carrying `.type`/`.message`/`.traceback`/`.stdout`.
   */
  runPython(py: string, params?: Record<string, string>): Promise<any>;
  /** URL-synced view state (see FusedParams). */
  params: FusedParams;
  /** File contents as UTF-8 text. Rejects on failure. */
  readFile(path: string): Promise<string>;
  /** Atomically write UTF-8 text; resolves with a fresh stat. */
  writeFile(path: string, content: string, opts?: FusedWriteOpts): Promise<FusedStat>;
  /** File metadata (size/mtime/etc). */
  stat(path: string): Promise<FusedStat>;
  /** Sync: a URL serving the file's raw bytes (for <script>/<img>/download). */
  rawUrl(path: string): string;
}

declare var fused: Fused;
