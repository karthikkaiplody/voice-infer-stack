# Agents

An agent is a folder. It says who the assistant is and what it knows, so you can
change either without touching any Python.

```
agents/library/
  agent.toml          who it is, and which files it uses
  prompt.md           how it behaves: persona and rules, no facts
  knowledge/
    library.md        hours, loans, fines, cards, rooms
    catalog.md        the shelves: a few books per genre, and where to find them
    summaries.md      one short summary per book
```

The library and its shelves are made up. The books are real, and the summaries
are written for this demo.

Run one with `VOICE_AGENT=library` (`make live` already does). With no agent
selected, the assistant is the original general one from `config.py`, with no
knowledge lookup. That is what `make trace` and the benchmark use, so their
numbers stay comparable.

## What happens on each question

1. You finish speaking and the transcript is final.
2. **Knowledge lookup.** The agent searches its notes for the passages that
   share words with your question, and takes the best few
   (`VOICE_RETRIEVAL_TOP_K`, default 3).
3. Those notes are put in front of your question for the model, with an
   instruction to answer only from them, and to say so if they do not cover it.
4. The model answers. The lookup is not remembered: the next question gets its own.

The lookup is a stage on the waterfall. It is a local word-matching search, so
it takes well under a millisecond. The point of showing it is that it is *not*
where the time goes.

## Make your own

1. Copy the folder and rename its notes:

   ```bash
   cp -r agents/library agents/bakery
   mv agents/bakery/knowledge/library.md agents/bakery/knowledge/bakery.md
   ```

2. Edit `agents/bakery/agent.toml`. `id` must match the folder name.

   ```toml
   id = "bakery"
   title = "Corner Bakery order line"          # shown in the page header
   description = "Answers questions about opening hours, orders and allergens."
   prompt = "prompt.md"
   greeting = "Hello, this is the Corner Bakery. How can I help?"   # optional

   [knowledge]
   files = ["knowledge/bakery.md"]
   ```

3. Rewrite `prompt.md` (who it is and how it talks) and the notes.
4. `VOICE_AGENT=bakery make live`

Mistakes are caught when the agent loads, with a message that says what to fix:
an unknown key, a file outside the folder, a missing file, or a file that is too
large (prompt 8 KB, notes 400 KB, at most 20 files). Restart to pick up edits.

## Writing notes that get found

The search matches **words, not meaning**. "When do you close?" finds the
opening-hours note because both say *close* and *hours*. "Late fees" will not
find a note that only says *fines*.

So write the way people ask. Each `##` heading becomes one passage, and a line
that lists the words people actually say is the cheapest fix there is:

```markdown
## Fines and late fees
Fines are 10 cents a day for an overdue item, capped at 5 dollars.
People often ask: what are the late fees, how much is the fine, what happens if I return a book late.
```

- Keep a section to a few sentences (about 700 characters, longer ones are split).
- Put facts in the notes and behaviour in the prompt. A fact in the prompt is
  read on every turn and never looked up.
- A question that matches nothing is answered with "I do not have that
  information", which is the right outcome. Check that it does.
- Give the agent a job in the prompt, not only facts in the notes. The library
  agent could not suggest a book until its prompt said it may, and only from the
  notes. Small models are sensitive to the prompt's wording: a longer prompt once
  flipped a fine of 10 cents to 25. After you change one, ask your agent its
  factual questions again.

## What the page can see

The page sees timing, a count, and a label. It never sees your question or the
notes. The label is the section's name (`library.opening-hours`: the file name
and the heading), so keep headings free of anything private.
