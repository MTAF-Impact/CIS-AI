"""Shared Jakarta-flavored sample post text for integration tests, mirroring
scripts/seed_demo_data.py's narrative themes (ERP road pricing, MRT Fase 2 tree removal).

Kept in English on purpose - all-MiniLM-L6-v2 is English-only, and these constants exist to
exercise clustering/CIB-detection logic reliably, not to demo Bahasa Indonesia content (that's
scripts/seed_demo_data.py's job, and it's a separate decision - see the note there).

Post text intentionally keeps the literal phrases "hidden tax" and "secretly" - these are the
exact trigger keywords tests.fakes.FakeLLMClient's analyze_content() keys off of.
"""

ERP_POSTS = [
    "The new ERP congestion charge on Sudirman is a hidden tax on working families!",
    "This ERP road pricing plan is really just a hidden tax on drivers, wake up.",
    "I can't believe the city snuck in a hidden tax through this ERP gantry plan.",
]

TREE_REMOVAL_POSTS = [
    "The city is removing dozens of mature trees near Monas for MRT Fase 2 construction "
    "staging, environmental betrayal.",
    "Trees gone near Monas for MRT construction staging?! Absolute hypocrisy from the city.",
]
