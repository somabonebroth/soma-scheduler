"""
Soma PWA Patch — adds manifest, Apple meta tags, and icon links to all HTML templates.
Idempotent: re-run safely; any existing block bracketed by the markers below is replaced.
Run from your soma-scheduler repo folder: python3 add_pwa_tags.py
"""
import os
import re
import glob

START_MARKER = "<!-- PWA tags (managed) -->"
END_MARKER = "<!-- /PWA tags -->"

PWA_BLOCK = f"""{START_MARKER}
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Soma">
<meta name="theme-color" content="#000000">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="icon" type="image/svg+xml" href="/static/logo.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/static/logo-180.png">
<link rel="apple-touch-icon" sizes="192x192" href="/static/logo-192.png">
<link rel="apple-touch-icon" sizes="512x512" href="/static/logo-512.png">
{END_MARKER}"""

# Pattern matches the managed block so we can replace it on re-runs
managed_block_re = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
    re.DOTALL,
)
# Also match the OLD un-managed PWA tags (pre-icon-update) so we replace them
legacy_re = re.compile(
    r'<meta name="apple-mobile-web-app-capable"[^>]*>.*?<link rel="apple-touch-icon"[^>]*>',
    re.DOTALL,
)
# And the even older shorter version (just manifest + apple-touch-icon)
legacy_short_re = re.compile(
    r'<meta name="theme-color"[^>]*>\s*<link rel="manifest"[^>]*>\s*<link rel="apple-touch-icon"[^>]*>',
    re.DOTALL,
)


def patch(content):
    """Inject PWA manifest + meta tags into templates that lack them."""
    if managed_block_re.search(content):
        return managed_block_re.sub(PWA_BLOCK, content), "updated"
    if legacy_re.search(content):
        return legacy_re.sub(PWA_BLOCK, content), "replaced legacy"
    if legacy_short_re.search(content):
        return legacy_short_re.sub(PWA_BLOCK, content), "replaced short legacy"
    # Not present — insert after the viewport meta tag
    viewport_re = re.compile(r'(<meta name="viewport"[^>]*>)')
    if viewport_re.search(content):
        return viewport_re.sub(r'\1\n' + PWA_BLOCK, content, count=1), "inserted"
    return content, "no viewport tag — skipped"


templates_dir = os.path.join(os.path.dirname(__file__), "templates")
if not os.path.exists(templates_dir):
    print("Error: templates/ folder not found.")
    exit(1)

count = 0
for filepath in glob.glob(os.path.join(templates_dir, "*.html")):
    with open(filepath, "r") as f:
        original = f.read()
    new, action = patch(original)
    name = os.path.basename(filepath)
    if new != original:
        with open(filepath, "w") as f:
            f.write(new)
        count += 1
        print(f"  ✓ {name}: {action}")
    else:
        print(f"  -  {name}: {action}")

print(f"\nDone — {count} template(s) updated.")
