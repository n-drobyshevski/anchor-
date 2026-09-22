"""The gated research loop (phase-4 plan).

Nothing in this package may import a state writer, an outbound module, or
persona loading. Web text reaches exactly one model call -- distill --
and reaches the persona only as the text of a card the user adopted.
tests/test_research_isolation.py pins both rules against the AST.
"""
