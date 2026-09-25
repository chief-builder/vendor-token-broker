# Maintaining the documentation

The public Pages artifact is generated from the Markdown in `docs/`. Keep the source guides authoritative; do not hand-edit `docs/index.html`.

## Before publishing

1. Update the guide that owns the behavior. Link to the API reference instead of copying response tables into walkthroughs.
2. If an MCP claim changes, record the exact MCP revision, review date, owner, implementation status, and evidence in `security.md`.
3. Update `docs/site.json` when the software revision or review baseline changes. The generator does not read it, so also update the baseline lines in `overview.md`, `security.md`, and `mcp-integration.md`.
4. Rebuild and validate the static artifact:

   ```sh
   .venv/bin/python tools/build-pages.py
   .venv/bin/python tools/build-pages.py --check
   ```

5. Run the appropriate tests. Documentation examples that change the local flow should be exercised against the Docker stack.
6. Inspect the generated page at a desktop and narrow viewport. Confirm the Mermaid console has no errors, section links land on the intended heading, and code blocks do not expose real credentials.

The generator adds stable heading IDs, converts links among the guides into one-page deep links, and checks those anchors. It also checks that every listed source exists before rendering. The Mermaid CDN is pinned to a tested exact version (currently 11.12.0) in the generated page; update it deliberately and recheck rendering before changing that version.

## What the site publishes

The single page includes Overview, Quickstart, MCP Integration, API Reference, Deploy and Operate, Security and MCP Alignment, Design, Token Lifecycle, Smoke Tests, and the Redis ADR. `docs-site-review.md` is maintainer review evidence and is not part of the reader navigation.

The source repository is private while the companion GitHub Pages repository is public. Copy the generated `docs/index.html` to `vendor-token-broker-docs` only after local checks pass. Publish the source revision, software release status, MCP revision, and review date with the companion artifact so readers can tell what they are reading.

## Drift checks

CI should run the generator check on every documentation or source change. A future browser smoke job should load the generated artifact, wait for Mermaid, assert that every `.mermaid` block has an SVG, and fail on console errors. Keep that browser check separate from the broker's protocol tests: a rendered diagram does not prove interoperability.

The Docker test workflow should start each coordination profile with isolated Redis state or wait for the old leader lease to expire. With default settings a stopped standalone leader may retain its sweep lease for up to 120 seconds. This matters when validating the multi-replica sweep test.
