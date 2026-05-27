## Project purpose

基于 OpenDataLoader PDF 开源项目打造，目标替代小绿鲸的 PDF 翻译工具。

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- Apply first-principles thinking before proposing solutions or making changes. Start from the original problem, constraints, and desired outcome instead of copying common patterns or following prior paths by inertia.
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

- When proposing modification or refactor plans, do not provide compatibility-first or patch-style options by default. Prefer converging directly to the target state.
- Do not over-engineer. Choose the shortest implementation path that fully satisfies the requirement.
- Do not introduce fallback, downgrade, bypass, or side-route solutions that were not explicitly required. Avoid drifting business logic away from the original goal.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```



Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

------

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

### 5. Project Structure

- `rsc/` contains Python source code. The main PDF translation workflow lives in `rsc/translate_pdf.py`, and the FastAPI web server for local workbench API lives in `rsc/web_app.py`.
- `my-react-app/` contains the React-based frontend web workbench for file upload, translation job queues, event-driven log monitoring, and synchronized dual-column PDF preview.
- `original_PDF/` contains source PDFs waiting to be translated. Keep the directory with `.gitkeep` when it has no committed inputs.
- `processed_PDF/` contains generated translated PDFs. Treat these as output artifacts unless the task explicitly asks to keep or review a generated sample.
- `docs/` contains local reference material for OpenDataLoader PDF and Cloud Translation API. Consult it before changing translation behavior or API usage.
- Root configuration and guidance files stay at the repository root, including `.env`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`, and `readme.md`.
- Temporary investigation files such as `_translate_log.txt`, `_stdout.txt`, `_pdf_text_output.txt`, `_debug_output.txt`, `_debug_api.py`, and `_test_pdf_read.py` are local artifacts. Do not build new structure around them.

### 6.Commit & Pull Request Guidelines

- Commit messages follow Conventional Commits with a  Chinese summary, e.g. `feat: 搭建聊天导航扩展基础` or `fix: 修正pytorch中的数值转化问题`.
- Include 3–5 bullet points in the body describing key changes.
- PRs (or change summaries) should include:
  - A short scope description and affected files.
  - Screenshots or GIFs for UI changes.
  - Manual test steps and target sites (e.g. `chatgpt.com`).
