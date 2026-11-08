"""Refresh file-backed node choices when their folders change."""

from .refresh import install

NODE_CLASS_MAPPINGS = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

install()
