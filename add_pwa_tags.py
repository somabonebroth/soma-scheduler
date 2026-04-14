"""
Soma PWA Patch — adds manifest and Apple meta tags to all HTML templates.
Run from your soma-scheduler repo folder: python3 add_pwa_tags.py
"""
import os
import glob

PWA_TAGS = """<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Soma">
<meta name="theme-color" content="#4a6741">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/static/logo.jpg">"""

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
if not os.path.exists(templates_dir):
    print("Error: templates/ folder not found. Run this from your repo root.")
    exit(1)

count = 0
for filepath in glob.glob(os.path.join(templates_dir, "*.html")):
    with open(filepath, "r") as f:
        content = f.read()

    if "manifest.json" in content:
        print(f"  Skip: {os.path.basename(filepath)} (already has manifest)")
        continue

    # Insert after the viewport meta tag
    if '<meta name="viewport"' in content:
        old = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        # Handle variations
        for variant in [
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
            '<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">',
        ]:
            if variant in content:
                content = content.replace(variant, variant + "\n" + PWA_TAGS, 1)
                count += 1
                print(f"  ✓ Patched: {os.path.basename(filepath)}")
                break
        else:
            print(f"  ⚠ Skipped: {os.path.basename(filepath)} (viewport tag format not recognized)")
            continue
    else:
        print(f"  ⚠ Skipped: {os.path.basename(filepath)} (no viewport tag found)")
        continue

    with open(filepath, "w") as f:
        f.write(content)

print(f"\nDone — {count} template(s) patched.")
print("Commit and push to deploy.")
