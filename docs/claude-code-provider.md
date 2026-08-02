# Claude Code as a Waku provider — design proposal

Status: PROPOSAL. Nothing below is implemented — no `PROVIDERS` row, no adapter class, no
eval. This is a design note for Sean to accept, reject, or amend before any code gets written.

Verified facts this proposal rests on (checked against the CLI docs, not assumed):

- A Claude subscription (Pro/Max) is not an API credential. `ant auth login` gives you keyless
  OAuth, but requests made with it still bill as metered API usage. The **only** supported way
  to spend a subscription's included usage programmatically is through Claude Code itself,
  authenticated via its own `claude login`.
- The locally installed `claude` binary has a headless mode: `claude -p "prompt"` (or
  `--print`) runs non-interactively. `--output-format json` returns one JSON object with
  `result` (the final text), `session_id`, `num_turns`, `duration_ms`, `total_cost_usd`, a
  `usage` block (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`), and an optional per-model `modelUsage` cost breakdown.
  `--output-format stream-json` gives newline-delimited events instead, ending in the same
  `result` message. `--mcp-config <file-or-json>` attaches MCP servers for that run.

## 1. Why

Every turn Waku runs today is metered: `waku/loop/models.py`'s `anthropic` provider bills
`ANTHROPIC_API_KEY` per token. Anyone already paying for Claude Pro/Max is paying twice —
once for the subscription, once for Waku's API calls — even though Claude Code, running on
the same laptop, could route those tokens through the subscription's included usage instead.
This serves one specific person: **a Claude subscriber who wants Waku's loop, memory, and
tools, but doesn't want a second metered bill for the privilege.** It is not a replacement for
the `anthropic` provider — it's an alternative for people who'd rather spend a subscription
than a key.

## 2. Proposed shape

A third `Provider.kind` — `"claude-cli"` — alongside today's `"anthropic"` and `"openai"` wire
kinds in `waku/loop/models.py`:

```python
"claude-cli": Provider("claude-cli", "", None, "sonnet", "haiku"),
```

No API key env var (empty `key_env` — `get_client()`'s key check would need to skip this
branch). `get_client()` gains a third arm returning a new adapter, mirroring how
`OpenAICompatClient` (~60 lines) bridges the OpenAI wire format into the Anthropic shape the
loop expects:

```python
class ClaudeCLIClient:
    """Speaks the Anthropic Messages shape via subprocess calls to the locally
    installed `claude` binary. No new dependency — stdlib subprocess + the
    user's own Claude Code install, same pattern as shelling out to git."""

    def __init__(self, model: str, timeout: float = 120.0):
        self.model = model
        self.timeout = timeout
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        prompt = _flatten(system, messages)          # see §3 — the hard part
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--model", model]   # note: current CLI has no --max-turns; bound via timeout
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        ...  # parse JSON, map result/usage into the Anthropic response shape
```

Zero new Python dependencies — `subprocess` is stdlib, `claude` is the user's own binary. This
is the same footprint rule the repo already follows for `say` (macOS TTS) and `git`.

## 3. The hard honest tradeoff

Claude Code is itself a harness. When it's given tools — via `--mcp-config` or its own
built-ins — headless mode runs **its own internal agent loop** and only returns once, with
the final `result` text. It does not hand back per-turn `tool_use` content blocks the way the
raw Anthropic Messages API does. `waku/loop/agent.py`'s while-loop is built entirely around
that per-turn contract: it inspects `response.content` for `tool_use` blocks, calls
`tools.execute()` itself, and appends `tool_result`s before looping again. Claude Code's
headless mode doesn't expose that seam. So for this provider, **Waku's Loop pillar is
partially bypassed — a harness running inside a harness**, and that has to be named plainly,
not smoothed over.

Two ways to reconcile them:

**(a) Hand Waku's tools to Claude Code via `--mcp-config`.** Stand up a local MCP server
wrapping `waku/tools/registry.py` (Waku already has an `mcp` extra and an MCP *client* bridge
in `waku/tools/mcp_client.py` — this would be the mirror-image, an MCP *server*), pass it to
`claude -p ... --mcp-config waku-tools.json`, and let Claude Code's own loop call `create_event`
/ `save_note` / `send_message` directly. One subprocess call per Waku turn, however many tool
calls Claude Code makes internally. `run_loop()` becomes a single iteration: no `tool_use`
blocks come back, so `result.tool_calls` stays empty even on a turn that genuinely used tools —
the dashboard's Loop tab goes dark for exactly the thing it exists to show, unless the adapter
additionally parses `--output-format stream-json` for tool-call system events and re-emits them
through the loop's `observer` (a second, parallel event schema Waku doesn't otherwise carry).

**(b) Restrict Claude Code's own tools and keep Waku's loop dispatching.** Deny all tools on
the CLI side, keep `run_loop()` exactly as it is today, and have each iteration shell out to
`claude -p` fresh with the accumulated `messages` serialized into one prompt, asking for a
single structured tool call back (`--json-schema` or an in-prompt convention). This preserves
the loop pillar and the dashboard trace untouched — but pays a full process-spawn plus
whole-history resend on *every iteration*, gets none of Claude Code's own context management,
and turns a well-tuned agentic harness into an expensive one-shot text-completion function.

**Recommendation: (a).** The subscription is paying for Claude Code's harness — its tuned tool
loop, caching, compaction. Option (b) pays that harness's overhead (subprocess spawn, CLI
startup) on every single tool-dispatch iteration while using none of what makes it worth
using. (a) is the only shape that's actually cheaper and better than the status quo; it just
means being upfront that the Loop pillar's per-call visibility either goes dark or needs the
stream-json parsing work as a named follow-up, not a footnote.

## 4. What still works / what degrades

- **Tracing** — improves in one way, degrades in another. `--output-format json`'s
  `total_cost_usd` and `usage` block are real numbers from Claude Code itself, better than
  `waku/ops/pricing.py`'s derived-from-tokens estimate — `usage.jsonl` could record actual
  spend. But per-iteration granularity (`kind="llm"` events per loop turn, individual tool
  events) requires the stream-json parsing named in §3(a); without it, a turn that made five
  tool calls traces as one opaque line.
- **Retrieval gate + consolidation** (`waku/memory/retrieval_gate.py`, cheap-model passes) —
  these call `get_client(settings)` and `settings.small_model` today, same client as the main
  loop. Routing the gate's one-line yes/no through `claude -p` too means a subprocess spawn for
  every message just to decide whether to search memory — slow relative to a real API call to
  Haiku. The honest option is letting `small_model` stay on a real `ANTHROPIC_API_KEY` (or any
  other provider) even when the main loop runs `claude-cli` — which quietly means a
  "no-API-key" pitch still wants one API key around for the cheap path.
- **Latency** — every `claude -p` call pays CLI startup (CLAUDE.md discovery, MCP/hook/skill
  auto-loading) on top of subprocess spawn. `--bare` mode trims this but still isn't a raw
  API round trip.
- **Session/history mapping** — Waku already owns conversation state: `session.py` rebuilds a
  bounded history window into `messages` every turn, and the loop is stateless across turns by
  design. Claude Code's `--resume`/`--continue` maintains its *own* session store, scoped to the
  CLI's project directory, with no knowledge of Waku's memory or consolidation. Using
  `--resume` would create two divergent transcripts. The straightforward fix: treat every Waku
  turn as one fresh, stateless `claude -p` call — same as every other provider — and never
  invoke `--resume`.

## 5. Failure mode

Same bar as every existing provider path: fail open to a readable message, never a bare
traceback. Concretely: `claude` not on `PATH` → point at installing Claude Code and running
`claude login`, the same way `get_client()` today raises `SystemExit` naming the missing env
var; not logged in / subscription lapsed → surface Claude Code's own stderr, don't swallow it;
CLI hang → respect `WAKU_LLM_TIMEOUT` via `subprocess.run(..., timeout=...)`, converting a
`TimeoutExpired` into the same kind of guardrail `max_iterations` already provides; non-zero
exit or unparseable stdout → raise with the raw stderr attached, same spirit as
`OpenAICompatClient._create`'s "endpoint returned no choices" handling.

## 6. Footprint ladder placement

Rung 1 — **extend something that already exists.** One `PROVIDERS` row plus one adapter class
in `waku/loop/models.py`, same shape and same file as `OpenAICompatClient`. Zero new Python
dependencies (`subprocess` is stdlib); the `claude` binary is user-installed software Waku
shells out to, not a package Waku bundles — no CONTRIBUTING.md dependency discussion needed.

What could get this **declined** per CONTRIBUTING.md, and why it's worth naming now rather than
finding out in review:

- **"A behavior change with no deterministic eval."** Needs an offline `evals/deterministic/
  test_providers.py` case (monkeypatched `subprocess.run`, no real `claude` binary in CI),
  same pattern the existing PROVIDERS-table test already uses.
- **The real risk:** if option (a)'s loop-bypass is judged to violate "each pillar legible on
  its own" — a provider whose tool calls are invisible to the dashboard's Loop tab arguably
  muddies exactly the Harness+Loop story CONTRIBUTING.md protects ("frameworks that hide the
  loop"). This is a maintainer call, not a code-review nit — it should be flagged explicitly in
  any PR, not discovered there.

## 7. Open questions for the maintainer

- Is losing per-tool-call trace visibility (§3, option a) an acceptable cost, or does the
  stream-json parsing need to land in the same PR rather than as follow-up?
- Should `small_model` (retrieval gate + consolidation) be allowed to stay on a different,
  API-billed provider even when `WAKU_PROVIDER=claude-cli`, or must everything route through
  Claude Code for the "no separate API key" pitch to hold?
- Is a subprocess spawn per turn (and per gate-call, if small_model also uses Claude Code) an
  acceptable latency hit, or does this need an explicit opt-in rather than being a normal
  `WAKU_PROVIDER` choice?
- Should Waku always pass `--bare` (deterministic, ignores the user's own Claude Code
  CLAUDE.md/hooks/skills) or without it (inherits the user's global Claude Code config, which
  could double-apply instructions Waku's own SOUL.md already sets)?
- Does the `--mcp-config` MCP-server-for-Waku's-own-tools work (§3, option a) need to land
  before this is usable, or can `claude-cli` ship first with tools disabled and no-tool chat
  only, as a smaller first milestone?
