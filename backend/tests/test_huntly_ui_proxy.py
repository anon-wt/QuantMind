"""Regression coverage for Huntly mounted below the news API prefix."""

import pytest

from backend.services.api.routers.huntly_ui_proxy import (
    UI_PREFIX,
    rewrite_html,
    rewrite_router_bundle,
)


@pytest.mark.parametrize("options", ["t", "options$1"])
def test_router_options_keep_history_and_enable_subpath(options):
    # Shape of the real upstream createBrowserRouter bundle (React Router 6).
    source = (
        f"return router({{basename:null=={options}?void 0:{options}.basename,"
        f"history:browserHistory({{window:{options}.window}}),routes:routes}})"
    )
    rewritten = rewrite_router_bundle(source)
    assert f'basename:"{UI_PREFIX}",history:' in rewritten
    assert f"browserHistory({{window:{options}.window}}),routes:routes" in rewritten


def test_unknown_or_ambiguous_upstream_router_is_rejected():
    router = "basename:null==t?void 0:t.basename,history:history"
    for source in ("console.log('no router')", router + ";" + router):
        with pytest.raises(ValueError, match="Expected one Huntly browser router"):
            rewrite_router_bundle(source)


def test_signin_redirects_stay_under_proxy_without_changing_external_urls():
    source = (
        'basename:null==t?void 0:t.basename,history:history;'
        'window.location.href="/";globalThis.location.href="/signin?from=";'
        'window.location.href="https://example.com/";'
        'window.location.href="//example.com/"'
    )
    rewritten = rewrite_router_bundle(source)
    assert f'window.location.href="{UI_PREFIX}/"' in rewritten
    assert f'globalThis.location.href="{UI_PREFIX}/signin?from="' in rewritten
    assert 'window.location.href="https://example.com/"' in rewritten
    assert 'window.location.href="//example.com/"' in rewritten


def test_html_versions_only_main_bundle_and_preserves_asset_hashes():
    html = (
        '<html><head><script src="/static/js/main.abc123.js"></script>'
        '<script src="/static/js/787.def456.chunk.js"></script>'
        '<link href="/static/css/main.abc123.css" rel="stylesheet">'
        '<link href="/favicon.ico" rel="icon"></head><body></body></html>'
    )
    bootstrap = "<script>bootstrap();</script>"
    rewritten = rewrite_html(html, bootstrap)
    assert f'src="{UI_PREFIX}/static/js/main.abc123.js?qm_proxy=2"' in rewritten
    assert f'src="{UI_PREFIX}/static/js/787.def456.chunk.js"' in rewritten
    assert f'href="{UI_PREFIX}/static/css/main.abc123.css"' in rewritten
    assert f'href="{UI_PREFIX}/favicon.ico"' in rewritten
    assert rewritten.index(bootstrap) < rewritten.index('src="')
