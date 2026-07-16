# Deep-Analysis Backends — per-language landscape

> **STATUS: CLOSED 2026-07 — survey informed the batched single-context engine decision (shipped, decode_mode: batched); multi-context simultaneous decode = dead end; MLX NO-GO. See memory: multi-persona-pooling-and-metal-multicontext-bug.**

> Research artifact for the `DeepAnalysisBackend` seam (`agent/analysis_backends/`).
> Written 2026-06-18. **Only jedi (Python) is implemented this sprint** — this doc
> evaluates the other nine languages so each can be slotted in behind the seam when a
> benchmark demands it, without re-deriving the landscape.

## Why this exists

Ouroboros splits code analysis into two tiers:

- **Shallow** — tree-sitter symbol *definitions*. Already multi-language (the
  `agent/languages.py` registry + `_extract_generic_tree_sitter`). Self-contained,
  no toolchain, runs on a source string. This stays as-is for every language.
- **Deep** — *references* (find all usages), *go-to-definition*, *type inference*,
  *attribute/contract resolution*. These feed the call-graph ranking, the
  unresolved-reference scan, and the contract/copy-paste sibling-defect scan. Today
  this tier is **Python-only**. The seam makes it pluggable per language.

A backend plugs in behind `DeepAnalysisBackend` (`references()` + `attribute_accesses()`),
selected per-file by `languages.spec_for_path(path)`.

## The decisive axis: the effects model

Ouroboros operates on **`(file_path, content)` pairs pulled through an effects layer**,
frequently in a container that may **not** have the target project's toolchain, venv,
`node_modules`, built workspace, or compile database. So the question that ranks every
backend is not "is it accurate?" — they all are, in an IDE — but:

> **Can it resolve project-local references on an in-memory source string WITHOUT a
> fully provisioned, buildable project on disk?**

That axis splits the field cleanly, and it is *why jedi is first*: it is the only
mainstream deep backend that is (a) an in-process library and (b) resolves project-local
symbols from a bare string with no environment.

## Summary matrix (top-10 languages)

| Lang | Backend (2026) | Integration | Real type inference | Runtime needed to run | In-memory, no build? | Seam priority |
|---|---|---|---|---|---|---|
| **Python** | **jedi** | **in-proc library** | partial (infer) | none (pure Python) | **YES** (project-local) | **NOW** |
| JavaScript | ts-morph | in-proc Node library | yes (TS checker) | Node | **PARTIAL** (needs lib `.d.ts`/`@types` or → `any`) | later |
| TypeScript | ts-morph | in-proc Node library | yes (TS checker) | Node | **PARTIAL** (as above) | later |
| PHP | **Intelephense** | LSP (Node) | yes (built-in) | **Node only, no PHP** | **NO/PARTIAL** (stubs; graceful w/o deps) | later (cheapest LSP) |
| Ruby | ruby-lsp (Solargraph=legacy) | LSP (Ruby gem) | no (heuristic/RBS) | Ruby 3+ | PARTIAL (Ruby+gems; refs structurally limited) | later |
| C / C++ | clangd | LSP (+ libclang/LibTooling) | yes (clang) | clang | **PARTIAL** (same-TU only; cross-file needs DB+headers on disk) | later |
| Go | gopls | LSP (no lib API) | yes | **`go` SDK on PATH** | **NO** (toolchain-gated; won't start w/o `go`) | gated on toolchain |
| Rust | rust-analyzer | LSP (+ unstable `ra_ap_*`) | yes (own engine) | **cargo/rustc + sysroot** | **NO** (crate-graph-gated; detached = 1 file, no cross-file) | gated on toolchain |
| Java | eclipse.jdt.ls | LSP (JVM) | yes (Eclipse compiler) | **JDK 21 + Maven/Gradle** | **NO** (standalone = syntax-only) | gated on toolchain |
| C# | Roslyn LSP (csharp-language-server for standalone) | LSP (.NET) | yes (Roslyn) | **.NET SDK + restored sln** | **NO** (semantic model needs metadata refs) | gated on toolchain |

*(Shell/Bash: excluded — no deep backend needed; tree-sitter definitions + the
`module_fix` path already cover the ops-task class. Counts as the de-facto 11th but is
intentionally a non-entry.)*

## Per-language notes

**Python — jedi.** In-process library (`import jedi`). `Script(code=content, path=file_path)`
analyzes a string; auto-detects the project root (`.git`/`setup.py`/`requirements.txt`),
and `get_references` defaults to `scope='project'` — so **project-local references resolve
from a bare string**. The boundary: third-party/stdlib symbols resolve weakly without the
venv on `sys.path`, but the two consumers (PageRank related-file ranking and the
"referenced-but-never-defined-*in-project*" scan) want exactly project-local scope, so the
degradation is benign. jedi is in maintenance steady-state (its author shipped a Rust
successor, **Zuban**, Sept 2025 — worth watching, not adopting). pyright/pylsp are heavier
Node/LSP alternatives; rope is a refactoring complement. **Verdict: the natural fit; first.**

**JavaScript / TypeScript — ts-morph.** In-process Node library over the TS compiler API;
`findReferences()`, `getDefinitions()`, full type inference; supports `useInMemoryFileSystem`.
**Caveat:** cross-file project-local references between in-memory files resolve, but type
inference and resolution into dependencies need the lib `.d.ts` + dependency `@types` seeded
into the in-memory FS, or results collapse to `any`. The LSP alternative
(typescript-language-server over tsserver) needs a tsconfig + `node_modules`. **Verdict:
viable as an in-proc library if we seed lib stubs; second-most-friendly after jedi.**

**PHP — Intelephense.** The surprise standout among LSP servers: a **Node** server that
ships PHP **stubs** and needs **no PHP runtime and no `composer install`** to operate. It
reads file contents, resolves core/stub symbols on a single file, and indexes whatever
source is present; missing `vendor/` deps simply read as undefined (graceful). The most
container-friendly non-Python backend. (Phpactor is more Composer/PHP-environment-bound.)

**Ruby — ruby-lsp.** Shopify's ruby-lsp is the 2024→2026 default (Solargraph is the
slower-moving veteran). Both need Ruby installed + the project's gems for dependency-level
references; both fall back to core/stdlib + intra-file on a bare file. Structural limits
regardless of project state: **ruby-lsp's find-references is still constants-mostly**, and
neither offers compiler-grade type inference (Solargraph = RBS/YARD-driven, ruby-lsp =
heuristic). Usable for project-local symbols if Ruby+gems present; never as deep as the
compiled-language servers.

**C / C++ — clangd.** Degrades the *most gracefully* of the LSP servers: with no compile
database it still starts (simple `clang foo.cc` fallback) and gives same-TU navigation,
hover, and error-tolerant local go-to-def on a single in-memory file. But **cross-file
references / cross-TU go-to-def / full type inference need `compile_commands.json` AND the
`#include`d headers materialized on disk** (clangd reads includes from disk even when the
edited file is in-memory). An in-process route exists (libclang / LibTooling
`runToolOnCode`) but hits the same header constraint.

**Go — gopls.** LSP only (no in-process library API; an experimental, unsupported CLI
exists). **Hard-requires the `go` binary on PATH** — it shells out to `go list`/`go env` and
will not function without the SDK; no `go.mod` → a "limited support" ad-hoc mode the Go team
itself disclaims. With the SDK + a module present it's excellent and honors in-memory
overlays. **Toolchain-gated.**

**Rust — rust-analyzer.** The worst for partial analysis: **`cargo metadata` is on the
critical path**; it builds the full crate graph (+ proc-macro/`build.rs` expansion, + a
sysroot for std) before answering cross-file queries. Detached single-file mode works for
*one* file only — no cross-file refs, external crates never resolve. An unstable embeddable
library (`ra_ap_*`) and a hand-authored `rust-project.json` escape hatch exist, but both
still need a crate graph + sysroot. **Crate-graph-gated.**

**Java — eclipse.jdt.ls.** LSP server (separate JVM). **Requires JDK 21 to run** and a
Maven/Gradle/classpath-resolved workspace for anything beyond single-file syntax — a `.java`
file with no classpath gets "only syntax errors are reported." Best-in-class semantics, but
the heaviest toolchain dependency. **Needs a resolved project.**

**C# — Roslyn LSP.** The OmniSharp→Roslyn transition is settled (VS Code's C# extension
dropped OmniSharp). Microsoft's official Roslyn server is editor-extension-coupled
("experimental" standalone); community `csharp-language-server` is the practical headless
pick. Either way: **needs the .NET SDK + a `dotnet restore`'d MSBuild solution** — Roslyn's
semantic model (the source of references/inference) requires resolved metadata references.
**Needs a resolved project.**

## Conclusion & implementation order

1. **jedi (Python) — NOW.** Uniquely fits the effects model (in-process, project-local
   resolution from a string, no toolchain), and **SWE-bench Verified — the one deep-code
   benchmark on the path — is 100% Python** (`BENCHMARK_PATH.md`). This is the only backend
   implemented this sprint.
2. **The rest are LSP servers** (except ts-morph and the libclang route, which are in-proc
   libraries). They are **gated on two costs the seam does not pay yet**: an out-of-process
   LSP-client transport (JSON-RPC lifecycle, per-language server provisioning) and, for
   Go/Rust/Java/C#, **the target toolchain + a buildable/restored workspace inside the
   effects container**. None of the four target benchmarks needs deep *non-Python* analysis
   (SWE=Python, terminal-bench=shallow/shell, GAIA/tau²=not code), so building these is
   **deferred until a benchmark demands it**.
3. **If/when a non-Python deep backend is needed, order by host-friendliness:** ts-morph
   (JS/TS, in-proc, seed lib stubs) and Intelephense (PHP, Node-only, no runtime) are the
   cheapest; clangd (C/C++) is partial; **gopls/rust-analyzer/jdtls/Roslyn require
   provisioning the toolchain + a built workspace** — the expensive tier, and the work there
   is as much an *effects/provisioning* problem as an analysis one.

**Architectural payoff:** the seam means none of step 2/3 touches a single consumer — a new
backend is one module + one `get_backend` branch. The interface (`references` +
`attribute_accesses` over the `analysis_types` dataclasses) is the contract; everything
above it is language-agnostic already.

## Sources

- **Python/jedi:** jedi.readthedocs.io/en/latest/docs/api.html · github.com/davidhalter/jedi · github.com/zubanls/zuban
- **JS/TS/ts-morph:** ts-morph.com/navigation/finding-references · github.com/dsherret/ts-morph (issues #938, #1252) · github.com/typescript-language-server/typescript-language-server
- **PHP:** intelephense.com/docs · github.com/bmewburn/vscode-intelephense · phpactor.readthedocs.io
- **Ruby:** shopify.github.io/ruby-lsp · github.com/Shopify/ruby-lsp · github.com/castwide/solargraph
- **C/C++/clangd:** clangd.llvm.org/design/compile-commands · clangd.llvm.org/design/indexing · clangd.llvm.org/troubleshooting
- **Go/gopls:** go.dev/gopls/ · go.dev/blog/gopls-scalability · go.dev/gopls/features/navigation · golang/go#57979
- **Rust/rust-analyzer:** rust-analyzer.github.io/book/features.html · non_cargo_based_projects.html · rust-lang/rust-analyzer#12499, #14318
- **Java:** github.com/eclipse-jdtls/eclipse.jdt.ls (#1794, disc. 3191)
- **C#:** github.com/razzmatazz/csharp-language-server · dotnet/roslyn disc. 82317 · OmniSharp/omnisharp-roslyn#2663
