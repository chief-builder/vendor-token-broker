#!/usr/bin/env python3
"""Build docs/index.html for GitHub Pages from the reader-focused docs.

Usage:  pip install markdown && python tools/build-pages.py

Markdown is pre-rendered to static HTML here; mermaid diagrams render
client-side (mermaid from the jsDelivr CDN), theme-matched to the
visitor's light/dark preference. Re-run after editing any source
document listed in SECTIONS.

Publishing: this repo is private, so the generated docs/index.html is
served from the public companion repo `vendor-token-broker-docs`
(GitHub Pages: https://chief-builder.github.io/vendor-token-broker-docs/).
After rebuilding, copy index.html there and push.

Use ``python tools/build-pages.py --check`` in CI to verify that the checked-in
HTML is current and that generated internal anchors resolve.
"""

import html
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

SECTIONS = [
    (
        "overview",
        "Overview",
        "overview.md",
        "AI assistants in GitHub, Linear, Jira, and Cloudflare, as each signed-in person.",
    ),
    (
        "quickstart",
        "Quickstart",
        "quickstart.md",
        "Try it on your laptop: sign in, connect services, and call tools from Claude Code.",
    ),
    (
        "gateway",
        "MCP Gateway",
        "mcp-gateway.md",
        "How the gateway works, what your sign-in service must do, and how to configure it.",
    ),
    (
        "mcp",
        "Connect Your Own MCP Server",
        "mcp-integration.md",
        "Use the token broker from an MCP server or gateway you build yourself.",
    ),
    (
        "api",
        "Broker API",
        "api.md",
        "The broker's internal REST API: requests, answers, and what to do on each error.",
    ),
    (
        "operations",
        "Deploy and Operate",
        "operations.md",
        "Set up storage, configure the broker, and fix common problems.",
    ),
    (
        "security",
        "Security",
        "security.md",
        "Which protections are built in, which are partial, and what we tested.",
    ),
    (
        "design",
        "Design",
        "design.md",
        "The broker's full design: roles, rules, states, and failure handling.",
    ),
    (
        "lifecycle",
        "Token Lifecycle",
        "token-lifecycle.md",
        "Every path a connection takes, step by step: connect, refresh, disconnect, outages.",
    ),
    (
        "smoke-tests",
        "Smoke Tests",
        "smoke-tests.md",
        "A hands-on tour that checks each protection with curl and a browser.",
    ),
    (
        "adr",
        "Redis Decision",
        "adr/0001-redis-coordination.md",
        "Why Redis coordinates several brokers running side by side.",
    ),
]

SECTION_BY_FILE = {fname: sid for sid, _, fname, _ in SECTIONS}

MERMAID_BLOCK = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.S)


def render(md_path: Path, section_id: str) -> str:
    text = md_path.read_text()
    body = markdown.markdown(text, extensions=["fenced_code", "tables", "toc"])
    body = MERMAID_BLOCK.sub(r'<pre class="mermaid">\1</pre>', body)
    # The section wrapper supplies the visible document title.
    body = re.sub(r"^<h1 id=\"[^\"]+\">.*?</h1>\n?", "", body, count=1)

    # Source Markdown links to another guide become deep links in the one-page
    # artifact; links to repository files remain ordinary relative links.
    def rewrite_link(match: re.Match[str]) -> str:
        target = match.group(1)
        path, sep, fragment = target.partition("#")
        target_name = path.removeprefix("docs/")
        target_sid = SECTION_BY_FILE.get(target_name)
        if target_sid:
            return f'href="#{fragment if sep and fragment else target_sid}"'
        return match.group(0)

    body = re.sub(r'href="([^"]*)"', rewrite_link, body)
    return body


def build_page() -> str:
    sections_html = []
    nav_html = []
    for sid, title, fname, blurb in SECTIONS:
        body = render(DOCS / fname, sid)
        nav_html.append(f'<a href="#{sid}">{title}</a>')
        sections_html.append(
            f'<section id="{sid}">\n'
            f'<div class="section-head"><h1>{html.escape(title)}</h1>'
            f'<p class="blurb">{html.escape(blurb)}</p>'
            f'<p class="src">source: <code>docs/{fname}</code></p></div>\n'
            f"{body}\n</section>"
        )

    return TEMPLATE.replace("{{NAV}}", "\n".join(nav_html)).replace(
        "{{SECTIONS}}", "\n<hr class='sep'/>\n".join(sections_html)
    )


def validate(page: str) -> list[str]:
    errors: list[str] = []
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    for target in re.findall(r'href="#([^"]+)"', page):
        anchor = target.split("#", 1)[0]
        if anchor and anchor not in ids:
            errors.append(f"missing internal anchor: #{target}")
    return errors


def main() -> None:
    check = "--check" in sys.argv[1:]
    missing = [
        f"missing source: docs/{fname}"
        for _, _, fname, _ in SECTIONS
        if not (DOCS / fname).exists()
    ]
    if missing:
        raise SystemExit("\n".join(missing))
    page = build_page()
    errors = validate(page)
    if errors:
        raise SystemExit("\n".join(errors))
    if check:
        current = (DOCS / "index.html").read_text()
        if current != page:
            raise SystemExit("docs/index.html is stale; run tools/build-pages.py")
        print("docs/index.html is current; internal anchors resolve")
        return
    (DOCS / "index.html").write_text(page)
    print(f"wrote {DOCS / 'index.html'} ({len(page) // 1024} KiB)")


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MCP Gateway and Token Broker — Documentation</title>
<style>
  /* Beige / dark-grey theme: warm paper in light mode, charcoal in dark. */
  :root {
    --bg: #f4efe3; --fg: #2b2a26; --muted: #6f6a5e; --line: #d9d0bc;
    --code-bg: #ebe4d2; --accent: #8a6d3b; --nav-bg: #f4efe3ee;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #262521; --fg: #e9e2d0; --muted: #a49d8c; --line: #45423a;
      --code-bg: #322f29; --accent: #d3b578; --nav-bg: #262521ee;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  nav {
    position: sticky; top: 0; z-index: 10; backdrop-filter: blur(6px);
    background: var(--nav-bg); border-bottom: 1px solid var(--line);
    display: flex; gap: 1.25rem; align-items: baseline;
    padding: .65rem 1.25rem; flex-wrap: wrap;
  }
  nav .brand { font-weight: 700; margin-right: .75rem; }
  nav a { color: var(--accent); text-decoration: none; font-weight: 500; }
  nav a:hover { text-decoration: underline; }
  nav a:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 3px; }
  main { max-width: 980px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }
  .skip { position: absolute; left: -9999px; top: .5rem; background: var(--bg); color: var(--fg); padding: .4rem .7rem; z-index: 20; }
  .skip:focus { left: .75rem; }
  h1, h2, h3 { line-height: 1.25; }
  h1, h2, h3 { scroll-margin-top: 5rem; }
  section > h2 { border-bottom: 1px solid var(--line); padding-bottom: .3rem; margin-top: 2.2rem; }
  .section-head h1 { font-size: 2rem; margin-bottom: .2rem; color: var(--accent); }
  .blurb { color: var(--muted); margin: .2rem 0; }
  .src { color: var(--muted); font-size: .85rem; margin-top: 0; }
  hr.sep { border: none; border-top: 3px double var(--line); margin: 3.5rem 0; }
  code {
    background: var(--code-bg); padding: .15em .35em; border-radius: 5px;
    font: .875em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  pre {
    background: var(--code-bg); border: 1px solid var(--line);
    border-radius: 8px; padding: .9rem 1rem; overflow-x: auto;
  }
  pre code { background: none; padding: 0; }
  :not(pre) > code { overflow-wrap: anywhere; }   /* long paths wrap on phones */
  /* Diagrams get a wider figure than the text column on large screens, so
     they render near full size; on narrow screens they fit the viewport. */
  pre.mermaid {
    --fig: min(1400px, calc(100vw - 48px));
    background: none; border: none; display: flex; justify-content: center;
    overflow-x: auto; width: var(--fig);
    margin-inline: calc((100% - var(--fig)) / 2);
  }
  table { border-collapse: collapse; display: block; overflow-x: auto; }
  th, td { border: 1px solid var(--line); padding: .4rem .7rem; text-align: left; }
  th { background: var(--code-bg); }
  blockquote { border-left: 4px solid var(--line); margin-left: 0; padding-left: 1rem; color: var(--muted); }
  a { color: var(--accent); }
  footer { color: var(--muted); font-size: .85rem; border-top: 1px solid var(--line); padding-top: 1rem; margin-top: 3rem; }
</style>
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<nav>
  <span class="brand">vendor-token-broker</span>
  {{NAV}}
</nav>
<main id="content">
{{SECTIONS}}
<footer>Generated from <code>docs/*.md</code> by <code>tools/build-pages.py</code>.
Diagrams render client-side with mermaid.</footer>
</main>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.esm.min.mjs";
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  mermaid.initialize({ startOnLoad: true, theme: dark ? "dark" : "neutral" });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
