"""Adapt the upstream Huntly SPA to the existing news proxy subpath."""

import json
import re

UI_PREFIX = "/api/v1/news/huntly-ui"
UI_REVISION = "2"

# React Router's bundled createBrowserRouter reads options.basename here.
# Match the property contract, not Huntly's changing chunk hash/minified names.
_ROUTER_BASENAME = re.compile(
    r"\bbasename\s*:\s*null\s*==\s*(?P<opts>[\w$]+)\s*"
    r"\?\s*void\s*0\s*:\s*(?P=opts)\.basename\s*,\s*history\s*:"
)


def rewrite_router_bundle(source: str) -> str:
    """Set the SPA basename so initial loads, Links and history share a root.

    Refuse unknown upstream layouts rather than silently serving a broken SPA.
    Huntly's original image and its root-path routes remain unchanged.
    """
    rewritten, count = _ROUTER_BASENAME.subn(
        f"basename:{json.dumps(UI_PREFIX)},history:", source
    )
    if count != 1:
        raise ValueError(f"Expected one Huntly browser router, found {count}")
    # Huntly's sign-in/401 handlers use location.href rather than router links.
    return re.sub(
        r'((?:window|globalThis)\.location\.href\s*=\s*")/(?!/)',
        rf"\1{UI_PREFIX}/",
        rewritten,
    )


def rewrite_html(html: str, bootstrap_script: str) -> str:
    """Prefix assets and bypass bundles cached before the router fix."""
    html = html.replace('src="/static/', f'src="{UI_PREFIX}/static/')
    html = html.replace('href="/static/', f'href="{UI_PREFIX}/static/')
    for asset in ("favicon.ico", "apple-touch-icon.png", "site.webmanifest"):
        html = html.replace(f'href="/{asset}"', f'href="{UI_PREFIX}/{asset}"')
    html = re.sub(
        rf'(src="{re.escape(UI_PREFIX)}/static/js/main(?:\.[^/"?]+)?\.js)"',
        rf'\1?qm_proxy={UI_REVISION}"',
        html,
    )
    return html.replace("<head>", "<head>" + bootstrap_script, 1)
