#!/usr/bin/env python3
"""
buildsite.py — Build 3proxy website repos from .html.jinja templates.

Template syntax (Jinja2-like, but processed without external dependencies):
  {% set Title = "page title" %}    — set the page <title>
  {% include "path" %}               — include a file as raw text (resolved relative to
                                       template dir, then site root, then content dir)
  {% content "path" %}               — include from content repo doc/html/

Include resolution order for {% include %}:
  1. Relative to the template's directory
  2. Relative to the site root
  3. With ../3proxy/ substituted to content_dir path

For each site repository:
  1. Generates doc/ templates from every .html file in the content repo's
     doc/html/ directory, wrapping each with intro/postpage.html.
  2. Copies RTF files from content repo's doc/ to the site's doc/.
  3. Processes all .html.jinja templates through the template engine.

Templates from templates/ produce output at the site root
  (e.g. templates/index.html.jinja → index.html).
Templates from doc/ produce output inside doc/
  (e.g. doc/faqe.html.jinja → doc/faqe.html).

Usage:
  buildsite.py <content-dir> <site-dir> [<site-dir> ...]

Environment:
  CONTENT_DIR    Alternative way to specify content dir.
  TITLE_PREFIX   Page title prefix; inferred from the site directory name
                 when unset.
"""
import os
import re
import sys
import shutil


# ---- Regex patterns for template directives ----

TITLE_RE = re.compile(r'\{%\s*set\s+Title\s*=\s*"([^"]*)"\s*%\}')
INCLUDE_RE = re.compile(r'\{%\s*include\s+"([^"]+)"\s*%\}')
CONTENT_RE = re.compile(r'\{%\s*content\s+"([^"]+)"\s*%\}')

# Pattern to match Title in intro.html's <title> tag
TITLE_IN_HTML_RE = re.compile(r'Title(\s*</title>)')


def resolve_include(inc_path, template_dir, site_root, content_dir):
    """Resolve an include path against template dir, site root, and content dir.

    Returns the absolute path of the resolved file, or None.
    """
    # 1. Relative to template's directory
    candidate = os.path.normpath(os.path.join(template_dir, inc_path))
    if os.path.exists(candidate):
        return candidate

    # 2. Relative to site root
    candidate = os.path.normpath(os.path.join(site_root, inc_path))
    if os.path.exists(candidate):
        return candidate

    # 3. With ../3proxy/ substituted to content_dir
    candidate = os.path.normpath(re.sub(r'(\.\./)+3proxy/', '', inc_path))
    candidate = os.path.join(content_dir, candidate)
    candidate = os.path.normpath(candidate)
    if os.path.exists(candidate):
        return candidate

    # 4. Also check under templates/ (for conventions like include/)
    candidate = os.path.normpath(os.path.join(site_root, 'templates', inc_path))
    if os.path.exists(candidate):
        return candidate

    # 5. Try content_dir directly (fallback for doc/ templates)
    candidate = os.path.normpath(os.path.join(content_dir, inc_path))
    if os.path.exists(candidate):
        return candidate

    return None


def read_file_raw(path):
    """Read a file and return its content as a string."""
    with open(path, encoding='utf-8') as f:
        return f.read()


def include_raw(path, title=None):
    """Read a file as raw text. If it's intro.html, substitute Title."""
    content = read_file_raw(path)
    if title is not None and 'intro.html' in os.path.basename(path):
        content = TITLE_IN_HTML_RE.sub(title + r'\1', content)
    return content


def process_template(template_path, site_root, content_dir, output_path):
    """Process a single .html.jinja template and write the output."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    template_dir = os.path.dirname(template_path)
    text = read_file_raw(template_path)

    # Extract Title
    title_match = TITLE_RE.search(text)
    title = title_match.group(1) if title_match else ''
    text = TITLE_RE.sub('', text)

    # Process directives line by line
    out_lines = []
    for line in text.splitlines(True):
        # Check for {% include "..." %}
        m = INCLUDE_RE.match(line)
        if m:
            inc_path = m.group(1)
            resolved = resolve_include(inc_path, template_dir, site_root, content_dir)
            if resolved is None:
                print(f"  WARNING: cannot resolve include: {inc_path}", file=sys.stderr)
                continue
            content = include_raw(resolved, title=title)
            out_lines.append(content)
            continue

        # Check for {% content "..." %}
        m = CONTENT_RE.match(line)
        if m:
            inc_path = m.group(1)
            # Substitute ../3proxy/ with content_dir
            clean = re.sub(r'(\.\./)+3proxy/', '', inc_path)
            resolved = os.path.normpath(os.path.join(content_dir, clean))
            if not os.path.exists(resolved):
                print(f"  WARNING: cannot resolve content: {inc_path}", file=sys.stderr)
                continue
            out_lines.append(read_file_raw(resolved))
            continue

        # Regular line — pass through
        out_lines.append(line)

    output = ''.join(out_lines)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output)


# ---- CHANGELOG news update ----

CHANGELOG_HEADER_RE = re.compile(
    r'^3proxy-([\d.]+(?:\s+-\s+[^(]+)?)\s+(Released|Вышел)\s+(.+)$'
)

NEWS_BEGIN = '<!-- BEGIN NEWS -->'
NEWS_END = '<!-- END NEWS -->'


def parse_changelog(path):
    """Parse CHANGELOG file, return (version, full_date, html) or None."""
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    if not lines:
        return None

    m = CHANGELOG_HEADER_RE.match(lines[0].strip())
    if not m:
        return None

    version = m.group(1).strip()
    date_keyword = m.group(2)
    date_str = m.group(3).strip()
    full_date = f'{date_keyword} {date_str}'

    # Convert changelog entries to simple HTML
    html_parts = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            html_parts.append('<p>')
        elif stripped.startswith(('+', '-', '!')):
            html_parts.append(f'<br>{stripped}')
        else:
            html_parts.append(stripped)

    return (version, full_date, '\n'.join(html_parts))


def update_news_in_template(template_path, content_dir):
    """Update index.html.jinja with latest news from CHANGELOG if version changed.

    The old news content between BEGIN/END markers is moved after END
    as a history entry, keeping it in the list alongside older versions.
    Returns True if template was modified.
    """
    site_dir = os.path.dirname(os.path.dirname(template_path))
    site_name = os.path.basename(site_dir)
    is_russian = site_name.endswith('.ru')

    changelog_name = 'CHANGELOG.rus' if is_russian else 'CHANGELOG'
    changelog_path = os.path.join(content_dir, changelog_name)

    result = parse_changelog(changelog_path)
    if result is None:
        print(f'  WARNING: cannot parse {changelog_name}', file=sys.stderr)
        return False

    version, full_date, html_changelog = result

    with open(template_path, encoding='utf-8') as f:
        text = f.read()

    if NEWS_BEGIN not in text or NEWS_END not in text:
        print(f'  WARNING: {NEWS_BEGIN}/{NEWS_END} markers not found', file=sys.stderr)
        return False

    pattern = re.escape(NEWS_BEGIN) + r'(.*?)' + re.escape(NEWS_END)
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return False

    current_news = m.group(1)

    # If this version is already the latest, skip
    if re.search(r'<b>\s*' + re.escape(version) + r'\s*</b>', current_news):
        print(f'  News already at version {version}')
        return False

    # Extract old changelog from between markers (after </td></tr>)
    old_entry = ''
    td_end = current_news.find('</td></tr>')
    if td_end >= 0:
        rest = current_news[td_end + len('</td></tr>'):]
        b_match = re.search(r'(<b>.*)', rest, re.DOTALL)
        if b_match:
            old_entry = b_match.group(1).strip()

    # Build new section between markers
    label = 'Новости:' if is_russian else 'Hot news:'
    date_sep = '  ' if is_russian else ' '

    new_news = (
        f'\n<tr><td>{label}</td><td bgcolor="white">\n'
        f'\t<a href="//3proxy.ru/download/">3proxy {version}</a>{date_sep}{full_date}.\n'
        f'</td></tr><tr><td colspan="2" bgcolor="white">\n'
        f'<b>{version}</b>\n<p>\n'
        f'{html_changelog}\n'
    )

    # Move old entry to history (insert after END marker)
    if old_entry:
        end_pos = text.find(NEWS_END) + len(NEWS_END)
        text = text[:end_pos] + f'\n<p>\n{old_entry}' + text[end_pos:]

    # Replace between markers with new content
    text = re.sub(pattern, f'{NEWS_BEGIN}{new_news}{NEWS_END}', text,
                  flags=re.DOTALL, count=1)

    with open(template_path, 'w', encoding='utf-8') as f:
        f.write(text)

    print(f'  Updated news to version {version}')
    return True


def build_site(site_dir, content_dir):
    """Build one site repository."""
    site_name = os.path.basename(site_dir)

    # Title prefix: taken from TITLE_PREFIX when set, otherwise inferred from
    # the directory name, which is how the tree is laid out locally.
    title_prefix = os.environ.get('TITLE_PREFIX')
    if not title_prefix:
        if site_name.endswith('.ru'):
            title_prefix = "3proxy : Документация"
        else:
            title_prefix = "3proxy : Documentation"

    print()
    print("=" * 80)
    print(f"  Building {site_name}")
    print("=" * 80)

    # ------------------------------------------------------------------
    # 0. Update index.html.jinja from CHANGELOG if needed
    # ------------------------------------------------------------------
    index_template = os.path.join(site_dir, 'templates', 'index.html.jinja')
    if os.path.exists(index_template):
        print()
        print("--- Checking CHANGELOG for news updates ---")
        update_news_in_template(index_template, content_dir)

    # ------------------------------------------------------------------
    # 1. Generate doc/ templates from every .html file in content's doc/html/
    # ------------------------------------------------------------------
    print()
    print("--- Generating doc/ templates ---")

    # Remove old generated templates
    doc_in_dir = os.path.join(site_dir, 'doc')
    if os.path.isdir(doc_in_dir):
        for root, dirs, files in os.walk(doc_in_dir):
            for f in files:
                if f.endswith('.html.jinja'):
                    os.remove(os.path.join(root, f))

    src_html_dir = os.path.join(content_dir, 'doc', 'html')
    count = 0
    if os.path.isdir(src_html_dir):
        for root, dirs, files in os.walk(src_html_dir):
            rel_root = os.path.relpath(root, src_html_dir)
            for f in sorted(files):
                if not f.endswith('.html'):
                    continue

                # Compute target template path
                if rel_root == '.':
                    template_path = os.path.join(site_dir, 'doc', f + '.jinja')
                else:
                    template_path = os.path.join(site_dir, 'doc', rel_root, f + '.jinja')

                os.makedirs(os.path.dirname(template_path), exist_ok=True)

                # Compute depth for ../include/ path
                depth = len(rel_root.split(os.sep)) if rel_root != '.' else 0
                depth += 1
                ups = '../' * depth

                # Content file path relative to content_dir
                content_rel = os.path.join('doc/html', rel_root, f) if rel_root != '.' else os.path.join('doc/html', f)
                content_rel = os.path.normpath(content_rel)

                # Source filename without .html
                basename_part = f[:-5] if f.endswith('.html') else f

                with open(template_path, 'w', encoding='utf-8') as tf:
                    tf.write(f'{{% set Title = "{title_prefix} : {basename_part}" %}}\n')
                    tf.write(f'{{% include "{ups}templates/include/intro.html" %}}\n')
                    tf.write(f'{{% content "{content_rel}" %}}\n')
                    tf.write(f'{{% include "{ups}templates/include/postpage.html" %}}\n')
                count += 1

    print(f"  Generated {count} templates")

    # ------------------------------------------------------------------
    # 2. Copy RTF files from content's doc/ to site's doc/
    # ------------------------------------------------------------------
    print()
    print("--- Copying RTF files ---")

    src_doc_dir = os.path.join(content_dir, 'doc')
    count = 0
    if os.path.isdir(src_doc_dir):
        for root, dirs, files in os.walk(src_doc_dir):
            for f in files:
                if not f.endswith('.rtf'):
                    continue
                src_path = os.path.join(root, f)
                rel_path = os.path.relpath(src_path, src_doc_dir)
                dest = os.path.join(site_dir, 'doc', rel_path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src_path, dest)
                count += 1

    print(f"  Copied {count} RTF files")

    # ------------------------------------------------------------------
    # 3. Process all .html.jinja templates
    #
    # Output path: strip templates/ prefix (pages land at site root),
    # keep doc/ prefix as-is. Strip .jinja suffix.
    # ------------------------------------------------------------------
    print()
    print("--- Processing templates ---")

    count = 0
    for root, dirs, files in os.walk(site_dir):
        for f in sorted(files):
            if not f.endswith('.html.jinja'):
                continue

            template_path = os.path.join(root, f)
            rel_template = os.path.relpath(template_path, site_dir)

            # Map template path to output path:
            #   templates/X.html.jinja          →  X.html
            #   templates/a/b.html.jinja        →  a/b.html
            #   doc/X.html.jinja               →  doc/X.html
            output = rel_template[:-6]  # strip .jinja
            if output.startswith('templates/'):
                output = output[len('templates/'):]

            output_path = os.path.join(site_dir, output)
            print(f"  {rel_template}  ->  {output}")
            process_template(template_path, site_dir, content_dir, output_path)
            count += 1

    print(f"  Processed {count} templates")
    print()
    print(f"=== {site_name} build complete ===")


def main():
    if len(sys.argv) == 0 or '-h' in sys.argv or '--help' in sys.argv:
        print(__doc__)
        sys.exit(0)

    # Parse arguments
    args = sys.argv[1:]

    if os.environ.get('CONTENT_DIR'):
        content_dir = os.path.abspath(os.environ['CONTENT_DIR'])
    else:
        content_dir = os.path.abspath(args[0])
        args = args[1:]

    if not os.path.isdir(os.path.join(content_dir, 'doc', 'html')):
        print(f"ERROR: content dir '{content_dir}' missing doc/html/", file=sys.stderr)
        sys.exit(1)

    if not args:
        print("ERROR: no site directories specified", file=sys.stderr)
        sys.exit(1)

    for site_arg in args:
        site_dir = os.path.abspath(site_arg)
        if not os.path.isdir(os.path.join(site_dir, 'include')):
            print(f"ERROR: '{site_dir}' does not look like a site repo (missing include/)", file=sys.stderr)
            sys.exit(1)
        build_site(site_dir, content_dir)

    print()
    print("All builds complete.")


if __name__ == '__main__':
    main()
