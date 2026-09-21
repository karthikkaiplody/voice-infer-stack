# Part 1 · Swap and see

*Part 1 of the companion guide · [← README](../README.md) · [Part 2 · What to notice →](2-what-to-notice.md)*

The point of this repo is not reading about latency — it is watching the shape
of a turn change when you change one variable. Everything on this page is
hands-on.

## Without models or a microphone

After `make setup-demo`, `make demo` opens the page on recorded turns: six
scenarios you can replay, including a grounded answer with its knowledge lookup
and a turn that answered too early. It is the same page as `make live`; the switch
in the header moves between the two. The numbers are synthetic, so it teaches how
to read the page, not what your machine does.

## Without a microphone

Same pipeline, same stages, same spans, fed from a WAV file at real 20 ms
cadence. It runs the real models, so run `make setup` once first:

```bash
make trace                 # or: make trace FIXTURE=03-trailing-pause
```

It runs the utterance twice, says which run was cold, and prints the budget.
Record your own instead:

```bash
make record NAME=my-question SECONDS=7
make trace FIXTURE=my-question
```

Speak, then **stop talking and let the recording run on** for a second. That
trailing silence is not waste; it is what the turn detector needs in order to
decide you have finished. The script measures where your speech actually ends,
so the silence never inflates the result, and it will tell you something
specific about how you talk:

```
  longest pause       260 ms
  needs stop_secs >=  0.26 s   (below this the agent will cut you off)
```

## Turn a knob, watch the shape change

Every component is chosen in `pipeline/factory.py` and set from the environment, so a
swap is one variable, not an edit:

| Knob | Values | What it changes |
|---|---|---|
| `VOICE_STT_ENGINE` | `mlx`, `faster-whisper` | MLX is Apple Silicon only; faster-whisper runs anywhere |
| `VOICE_STT_MODEL` | any MLX Whisper id | `whisper-tiny` is ~15x faster than `large-v3-turbo-q4` here |
| `VOICE_TTS_ENGINE` | `kokoro`, `piper` | both local, both download on first use |
| `VOICE_LLM_MODEL` | any Ollama model | `make live` uses `llama3.2:3b`. `llama3.2:1b` is faster but misreads the library's notes (it says the library is closed on Sundays) |
| `VOICE_AGENT` | a folder name under `agents/` | who the assistant is and what it knows. Empty means the original general assistant with no lookup. `make live` sets `library`; see [`agents/README.md`](../agents/README.md) |
| `VOICE_RETRIEVAL_TOP_K` | integer | how many notes the agent hands the model per question. More is more to read, and a longer prompt |
| `VOICE_VAD_STOP_SECS` | seconds | how long to wait in silence before deciding you are done |
| `VOICE_VAD_MIN_VOLUME` | 0–1 | how loud counts as speech. Machine-specific; see the meter |
| `VOICE_USER_SPEECH_TIMEOUT` | seconds | a second timer stacked on the first. See [insight 3](2-what-to-notice.md#3-two-default-timeouts-can-cost-more-than-any-model) |
| `VOICE_STT_TTFS_P99` | seconds | the safety-net timer from insight 3. Pipecat's own default is `1.0`. In our runs raising it changed nothing, because a transcript flagged final ends that wait first |
| `VOICE_USE_SMART_TURN` | `1` | swap the silence timer for a learned turn-detection model, and watch the first row change shape |

```bash
VOICE_TTS_ENGINE=piper VOICE_LLM_MODEL=qwen2.5:0.5b make live
```

Every trace records the stack it ran on, so a result can never be separated
from the configuration that produced it:

```
stack: stt=mlx:mlx-community/whisper-large-v3-turbo-q4  llm=ollama:llama3.2:3b  tts=kokoro:af_heart
       vad=silero  endpoint=vad_timeout@0.5s
```

**What is not swappable, and why.** Silero is the only local VAD Pipecat ships,
so there is no second option to offer. Both Whisper engines are *segmented*:
neither emits a partial transcript while you are still speaking. That is a
property of the model, not the engine, and no swap in `pipeline/factory.py` changes it.
It is also a large part of what hosted streaming speech-to-text actually sells
you.

## Give it something to know

The library agent is not a model trick, it is a folder: a prompt, a greeting and a
markdown file of facts. On every question it looks up the notes that share words
with what you asked, hands them to the model, and answers only from them. Ask it
something the notes do not cover and it should say so instead of guessing.

```bash
VOICE_AGENT=library make live     # what make live already does
VOICE_AGENT= make live            # the general assistant: no notes, no lookup
```

To write your own, copy `agents/library/` and change the notes. The guide is in
[`agents/README.md`](../agents/README.md). The one thing to know first: the search
matches words, not meaning, so the notes need the words people actually say.

## Three swaps to try, in order

Each is one or two variables on `make trace`, and each moves the budget in a
different way. Run `make trace` first for a baseline, then these, and compare the
rows, not the absolute numbers: the ones quoted below are from one Apple Silicon
Mac, one run each, and yours will differ.

**1. Shorten the speech timeout.**
```bash
VOICE_STT_MODEL=mlx-community/whisper-tiny make trace
VOICE_STT_MODEL=mlx-community/whisper-tiny VOICE_USER_SPEECH_TIMEOUT=0.2 make trace
```
The turn-detection row goes from about 1,100 ms to about 700 ms and the whole
turn from about 1,720 ms to about 1,300 ms, with no model changed. Use the tiny
model for this one: with the default large model the turn is waiting on a
transcript that takes about a second anyway, so the timer is not what holds it up
and the row barely moves. The wait ends when the last of its conditions is met.
This is [insight 3](2-what-to-notice.md#3-two-default-timeouts-can-cost-more-than-any-model).

**2. Cut the wait too short.**
`VOICE_VAD_STOP_SECS=0.2 VOICE_USER_SPEECH_TIMEOUT=0.0 make trace`
The turn-detection row collapses to about 200 ms, and the turn is no faster than
where you started. The agent decided you were done before speech-to-text had
returned everything you said, so the model started on what it had. The rest
arrived while it was answering, and the answer was thrown away and generated
again: the budget prints `llm 2x - work was discarded`. You need both variables.
With only `VOICE_VAD_STOP_SECS=0.2` the default 0.6 s timeout still lets you
resume, and nothing is discarded. This is
[insight 2](2-what-to-notice.md#2-you-cannot-just-turn-it-down). The page shows
the same thing without running anything: replay *Answered too early*.

**3. The null result.**
`VOICE_LLM_MODEL=llama3.2:1b make trace` (needs `ollama pull llama3.2:1b`)
A model a third the size does not make the turn faster. In our runs it got slower,
about 1,900 ms against 1,710 ms, and the two rows at about 1,100 ms did not move.
The model was never the biggest line item, which is the thesis of the talk in one
command.

---

*Next: [Part 2 · What to notice →](2-what-to-notice.md)*
