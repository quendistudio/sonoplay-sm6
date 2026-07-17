"""Renderer profiles — URL/stream strategy per device type."""

# Legacy Cambridge Stream Magic (e.g. SM6) uses a custom DLNA stack that cannot
# fetch Plex :32400 / plex.direct URLs. Newer Cambridge streamers (CXN, Evo, …)
# follow standard conventions and do not need the :32469 workaround.


def is_sm6_like(device) -> bool:
    """Detect an SM6 (or legacy Stream Magic) tolerantly."""
    manufacturer = (getattr(device, "manufacturer", None) or "").casefold().strip()
    model = (
        getattr(device, "model_name", None)
        or getattr(device, "model", None)
        or ""
    ).casefold().strip()
    name = (getattr(device, "name", None) or "").casefold().strip()

    if "cambridge" in manufacturer and (
        model.startswith("stream magic") or "stream magic" in model
    ):
        return True
    return "stream magic" in name


def _legacy_cambridge_stream_magic(device) -> bool:
    return is_sm6_like(device)


def needs_plex_dlna_stream_url(device) -> bool:
    """True for legacy Stream Magic renderers that need Plex :32469 object URLs."""
    return is_sm6_like(device)


def is_legacy_cambridge_stream_magic(device) -> bool:
    """Public alias used by volume routing."""
    return is_sm6_like(device)
