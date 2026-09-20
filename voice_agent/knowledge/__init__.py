"""What the agent knows, and how it looks things up.

    agents.py     loads an agent from `agents/<name>/` (prompt, greeting, notes)
    search.py     splits notes into passages and ranks them against a question (BM25)
    retrieval.py  the pipeline stage that adds the best notes to the user's question
"""
