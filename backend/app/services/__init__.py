"""Myra services layer — real subsystems behind the agent tools.

Mirrors the MODELS-independent "services" grouping of the target architecture.
Each module wraps one external capability the agent drives via its tools:
browser (Playwright), git/github, databases, and local preview.
"""
