"""Entry point for the Wan2GP Timeline Editor plugin.

This file must stay at repository root so Wan2GP can import
`<plugin_folder>.plugin` during plugin installation/loading.
"""

from __future__ import annotations

import gradio as gr

# Wan2GP plugin base can vary slightly across versions/import paths.
# Keep graceful fallbacks so the module remains importable.
try:
    from shared.plugins.base import WAN2GPPlugin  # type: ignore
except Exception:  # pragma: no cover - fallback path
    try:
        from shared.plugins.base_plugin import WAN2GPPlugin  # type: ignore
    except Exception:  # pragma: no cover - local/dev fallback
        class WAN2GPPlugin:  # type: ignore
            """Fallback base for static checks outside Wan2GP runtime."""

            pass


class TimelineEditorPlugin(WAN2GPPlugin):
    """Minimal installable plugin shell for Wan2GP Timeline Editor."""

    name = "Wan2GP Timeline Editor"

    def setup_ui(self, app=None):
        """Create the Timeline Editor tab UI.

        The implementation is intentionally lightweight for MVP wiring.
        Timeline logic (multi-track model, FFmpeg graph compiler, etc.)
        will be added incrementally in future milestones.
        """
        with gr.Tab("Timeline Editor"):
            gr.Markdown(
                """
                # Wan2GP Timeline Editor (MVP scaffold)

                This plugin is correctly structured for Wan2GP URL installation:
                - `plugin.py` at repository root
                - importable package via `__init__.py`
                - plugin metadata in `plugin_info.json`

                Next step: implement media bin, timeline interactions,
                inspector and FFmpeg export pipeline.
                """
            )


# Optional alias some loaders/tools may expect.
Plugin = TimelineEditorPlugin
