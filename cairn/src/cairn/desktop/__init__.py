"""Cairn desktop app: a native window around the Cairn web UI plus a settings editor.

The app runs the Cairn server in-process, supervises the dispatcher as a child process,
and keeps API keys in the Windows Credential Manager (never in plain-text files).
"""
