"""Phase 3: chat answers render as markdown — executed for real via node
when available, with a static fallback assertion for the wiring."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HARNESS = """
const el = { value: "", addEventListener() {}, appendChild() {}, scrollTop: 0,
             querySelector: () => ({ disabled: false }) };
globalThis.document = { getElementById: () => el, createElement: () => el,
                        querySelectorAll: () => [] };
globalThis.localStorage = { getItem: () => "", setItem() {} };
"""

CASES = [
    # (input, expected substring, description)
    ("Step 1: install.\nStep 2: run.", "Step 1: install.<br>Step 2: run.<br>", "plain lines"),
    ("**Important:** do this", "<strong>Important:</strong> do this<br>", "bold"),
    ("use `npm install` now", "use <code>npm install</code> now<br>", "inline code"),
    ("1. First\n2. Second", "<ol><li>First</li><li>Second</li></ol>", "numbered list"),
    ("- one\n- two", "<ul><li>one</li><li>two</li></ul>", "bullet list"),
    ("## Summary", "<strong>Summary</strong><br>", "heading"),
    ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;<br>", "XSS stays escaped"),
]


def test_render_markdown_behavior():
    if shutil.which("node") is None:
        static_wiring_fallback()
        return
    js_source = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    checks = "\n".join(
        f'assertEq({text!r}, {needle!r}, {desc!r});' for text, needle, desc in CASES
    )
    script = (
        HARNESS
        + js_source
        + "\nfunction assertEq(input, needle, desc) {"
        + "\n  const out = renderMarkdown(input);"
        + "\n  if (!out.includes(needle)) {"
        + "\n    console.error(`FAIL [${desc}]:\\n  got: ${out}\\n  want substring: ${needle}`);"
        + "\n    process.exit(1);"
        + "\n  }"
        + "\n}"
        + f"\n{checks}\nconsole.log('all markdown cases passed');"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"node renderMarkdown failed:\n{result.stdout}\n{result.stderr}"
    assert "all markdown cases passed" in result.stdout


def static_wiring_fallback():
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function renderMarkdown" in js
    assert "renderMarkdown(text)" in js


def test_bubble_uses_render_markdown():
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "renderMarkdown(text)" in js
