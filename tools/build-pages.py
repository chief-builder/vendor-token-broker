#!/usr/bin/env python3
"""Build docs/index.html for GitHub Pages from the three documentation
sources: design.md, token-lifecycle.md, smoke-tests.md.

Usage:  pip install markdown && python tools/build-pages.py

Markdown is pre-rendered to static HTML here; mermaid diagrams render
client-side (mermaid from the jsDelivr CDN), theme-matched to the
visitor's light/dark preference. Re-run after editing any of the three
source documents.

Publishing: this repo is private, so the generated docs/index.html is
served from the public companion repo `vendor-token-broker-docs`
(GitHub Pages: https://chief-builder.github.io/vendor-token-broker-docs/).
After rebuilding, copy index.html there and push.
"""
import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

SECTIONS = [
    ("design", "Design", "design.md",
     "The normative design: role, standards basis, trust boundaries, API, "
     "state machine, failure modes, deployment profiles."),
    ("lifecycle", "Token Lifecycle", "token-lifecycle.md",
     "Every path a grant takes, as sequence diagrams — consent, refresh, "
     "multi-replica takeover, revocation, outages."),
    ("smoke-tests", "Smoke Tests", "smoke-tests.md",
     "The illustrated hands-on walkthrough: every security property "
     "verified with curl and a browser."),
]

MERMAID_BLOCK = re.compile(
    r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.S)


def render(md_path: Path, section_id: str) -> str:
    text = md_path.read_text()
    body = markdown.markdown(text, extensions=["fenced_code", "tables"])
    body = MERMAID_BLOCK.sub(r'<pre class="mermaid">\1</pre>', body)
    # Make cross-references between the three documents jump to sections.
    for sid, _, fname, _ in SECTIONS:
        body = body.replace(f"<code>{fname}</code>",
                            f'<a href="#{sid}"><code>{fname}</code></a>')
    return body


def main() -> None:
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
            f"{body}\n</section>")

    page = TEMPLATE.replace("{{NAV}}", "\n".join(nav_html)) \
                   .replace("{{SECTIONS}}", "\n<hr class='sep'/>\n".join(sections_html))
    (DOCS / "index.html").write_text(page)
    print(f"wrote {DOCS / 'index.html'} ({len(page)//1024} KiB)")


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vendor Token Broker — Documentation</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --line: #d0d7de;
    --code-bg: #f6f8fa; --accent: #6366f1; --nav-bg: #ffffffee;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d;
      --code-bg: #161b22; --accent: #818cf8; --nav-bg: #0d1117ee;
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
  main { max-width: 980px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }
  h1, h2, h3 { line-height: 1.25; }
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
  pre.mermaid {
    background: none; border: none; display: flex; justify-content: center;
    overflow-x: auto;
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
<nav>
  <span class="brand">vendor-token-broker</span>
  {{NAV}}
  <a href="https://github.com/chief-builder/vendor-token-broker">repo</a>
</nav>
<main>
{{SECTIONS}}
<footer>Generated from <code>docs/*.md</code> by <code>tools/build-pages.py</code>.
Diagrams render client-side with mermaid.</footer>
</main>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  mermaid.initialize({ startOnLoad: true, theme: dark ? "dark" : "default" });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
