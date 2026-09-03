# AGENTIC DIRECTIVE

> Keep AGENTS.md and CLAUDE.md identical.

## CODING ENVIRONMENT

- **Package Manager**: Use `uv` for lightning-fast dependency management and virtual environment creation. Use `uv run` for executing scripts.
- **Python Version**: Python 3.11 or higher.
- **Core Stack**: 
  - `aiogram` (v3.x) for Telegram integration.
  - `pyromax` for Max Messenger integration.
  - `SQLAlchemy` (async) + `aiosqlite` for state and mapping persistence (SQLite is sufficient for this scale).
  - `pydantic` and `pydantic-settings` for robust configuration management and validation.
- **Code Quality**: Use `ruff` for both linting and formatting. Run `uv run ruff format` and `uv run ruff check --fix` before committing.
- **Type Checking**: Strictly enforce static typing using `mypy` or `pyright`. Do not use `# type: ignore` without an explicit justification comment. Fix the underlying type issue.
- **Environment Variables**: Manage configuration via a `.env` file (provide a `.env.example`). Never hardcode credentials, tokens, or API keys.

## IDENTITY & CONTEXT

- You are an expert Async Python Systems Engineer and Integration Architect.
- Goal: Build a highly resilient, non-blocking, zero-defect message bridge between two distinct messaging protocols.
- Code: Prioritize modularity, separation of concerns, and robust error handling. Keep the codebase minimal and modular.

## ARCHITECTURE PRINCIPLES

- **Separation of Concerns (SoC)**: Isolate API clients strictly. Create distinct modules for `telegram_adapter`, `max_adapter`, and a central `bridge_controller` (or message bus) that orchestrates between them. Providers/Adapters must not import directly from each other.
- **Unified Message Interface**: Design a common intermediate data structure (e.g., `NormalizedMessage`, `NormalizedAttachment`) that both platform adapters translate to and from. The core logic should only interact with these normalized objects.
- **Attachment Pipeline**: Implement an async streaming pipeline for files. Download files to temporary storage (in-memory or tmpfs) and upload them sequentially. Pay strict attention to API payload limits (e.g., Telegram's bot upload limits) and implement graceful fallbacks, logging, or splitting if limits are exceeded.
- **State Mapping & Persistence**: Maintain a robust persistence layer mapping `telegram_chat_id` <-> `max_chat_id`. To support full functionality (like replies, edits, or deletes), maintain a mapping of `telegram_msg_id` <-> `max_msg_id`.
- **Concurrency & Rate Limiting**: Both Telegram and Max APIs have specific rate limits. Implement `asyncio` Semaphores or task queues in the adapters to prevent HTTP 429 (Too Many Requests) bans or flood waits.
- **Resilience & Fault Tolerance**: Network calls fail. Wrap API calls in retry loops (e.g., using `tenacity`) for transient errors. A failure to forward one specific message or attachment must not crash the listener loops of either client.
- **Logging**: Use structured async logging. Log distinct tracking IDs for cross-platform message flows to trace a message's lifecycle from Max -> Core -> Telegram (and vice versa).

## COGNITIVE WORKFLOW

1. **ANALYZE**: Understand the specific async event loops, webhook/polling mechanisms of `aiogram`, and the event listeners of `pyromax`. Do not guess API structures.
2. **PLAN**: Define the `NormalizedMessage` interface and database schema before implementing the platform-specific adapters. Map out the attachment flow.
3. **EXECUTE**: Build iteratively. Fix the cause, not the symptom.
   - *Phase 1*: Pure text forwarding between mapped chats.
   - *Phase 2*: Attachment forwarding (photos, documents).
   - *Phase 3*: Edge cases (replies, long messages, rate limit handling).
4. **VERIFY**: Ensure the bot can run concurrently listening to both Telegram and Max events without blocking the main thread.
5. **SPECIFICITY**: Do exactly as much as asked.
6. **PROPAGATION**: Changes to the intermediate message structure must be correctly updated in both platform adapters simultaneously.

## SUMMARY STANDARDS

- Summaries must be technical and granular.
- Include: [Files Changed], [Integration Points Altered], [Verification Method], [Residual Risks] (if no residual risks then say none).

## TOOLS

- Prefer built-in tools over manual workflows. Check tool availability before use.
