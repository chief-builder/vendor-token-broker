# Maintaining the documentation

This page is for people who maintain the docs site.

- The Markdown files in `docs/` are the source of truth.
- `tools/build-pages.py` builds the public Pages artifact, `docs/index.html`, from them.
- Never edit `docs/index.html` by hand.

## Before publishing

Work through these steps for every docs change.

1. Update the page that owns the behavior. Link to the API reference instead of copying its response tables into walkthroughs.
2. If an MCP claim changes, record these in `security.md`: the exact MCP revision, review date, owner, implementation status, and evidence.
3. When the software revision or review baseline changes, update `docs/site.json`. The generator does not read that file, so also update the review date and revision lines in `overview.md` (Status), `security.md` (top line and Verification record), and `mcp-integration.md` (top line).
4. Rebuild and check the static artifact:

   ```sh
   .venv/bin/python tools/build-pages.py
   .venv/bin/python tools/build-pages.py --check
   ```

5. Run the tests that apply. If a docs example changes the local flow, try it against the Docker stack.
6. Open the generated page at desktop width and at a narrow width. Check that:
   - the browser console shows no Mermaid errors
   - section links land on the right heading
   - code blocks contain no real credentials

**What the generator does:**

- adds stable heading IDs
- turns links between the pages into deep links within the one page, and checks that those anchors exist
- checks that every listed source file exists before it renders

The generated page loads Mermaid from a CDN, pinned to an exact tested version (currently 11.12.0). Change that version only on purpose, and recheck the rendering when you do.

## What the site publishes

The site is one page. It includes Overview, Quickstart, MCP Gateway, Connect Your Own MCP Server, Broker API, Deploy and Operate, Security, Design, Token Lifecycle, Smoke Tests, and the Redis Decision.

**Where it goes:**

- The site is served from a companion public repository, `vendor-token-broker-docs` (GitHub Pages). This source repository is public too.
- Copy the generated `docs/index.html` there only after the local checks pass.
- Publish these with it, so readers know what they are reading: the source revision, software release status, MCP revision, and review date.

## Drift checks

These checks catch docs that fall out of step with the code.

- CI runs the generator check (`tools/build-pages.py --check`) on every push.
- Not automated yet: a browser job that would load the generated artifact, wait for Mermaid, check that every `.mermaid` block has an SVG, and fail on console errors. Until then, do this check by hand (step 6 above).
- Keep that browser check separate from the broker's protocol tests. A diagram that renders does not prove interoperability.

**Redis state in the Docker test workflow.** Start each coordination profile with its own clean Redis state, or wait for the old leader lease to expire. With default settings, a stopped standalone leader can hold its sweep lease for up to 120 seconds. This matters when you check the multi-replica sweep test.
