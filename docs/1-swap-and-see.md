# Part 1 · Swap and see

*Part 1 of the companion guide · [← README](../README.md) · [Part 2 · What to notice →](2-what-to-notice.md)*

The point of this repo is not reading about latency — it is watching the shape
of a turn change when you change one variable. Everything on this page is
hands-on.

## Without a microphone

Same pipeline, same stages, same spans, fed from a WAV file at real 20 ms
cadence:

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

Every component is chosen in `factory.py` and set from the environment, so a
swap is one variable, not an edit:

| Knob | Values | What it changes |
|---|---|---|
| `VOICE_STT_ENGINE` | `mlx`, `faster-whisper` | MLX is Apple Silicon only; faster-whisper runs anywhere |
| `VOICE_STT_MODEL` | any MLX Whisper id | `whisper-tiny` is ~15x faster than `large-v3-turbo-q4` here |
| `VOICE_TTS_ENGINE` | `kokoro`, `piper` | both local, both download on first use |
| `VOICE_LLM_MODEL` | any Ollama model | `llama3.2:1b`, `qwen2.5:0.5b`, … |
| `VOICE_VAD_STOP_SECS` | seconds | how long to wait in silence before deciding you are done |
| `VOICE_VAD_MIN_VOLUME` | 0–1 | how loud counts as speech. Machine-specific; see the meter |
| `VOICE_USER_SPEECH_TIMEOUT` | seconds | a second timer stacked on the first. See [insight 3](2-what-to-notice.md#3-two-default-timeouts-can-cost-more-than-any-model) |
| `VOICE_STT_TTFS_P99` | seconds | the safety-net timer from insight 3. Set it to `1.0` to recreate the stock wait |
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
property of the model, not the engine, and no swap in `factory.py` changes it.
It is also a large part of what hosted streaming speech-to-text actually sells
you.

## Three swaps to try, in order

Each is one variable on `make trace`, and each moves the budget in a
different way. Run `make trace` first for a baseline, then these, and
compare the rows.

**1. Recreate the stock wait.**
`VOICE_STT_TTFS_P99=1.0 VOICE_USER_SPEECH_TIMEOUT=0.6 make trace`
Pipecat's own defaults, on a local stack: the turn-detection row grows by
hundreds of milliseconds while no model changes. This is
[insight 3](2-what-to-notice.md#3-two-default-timeouts-can-cost-more-than-any-model).

**2. Cut the silence dial too short.**
`VOICE_VAD_STOP_SECS=0.2 make trace`
Below the fixture's natural clause pause, the detector fires mid-sentence:
the agent answers a fragment, throws the work away, and answers again. The
budget marks the discarded generation. This is
[insight 2](2-what-to-notice.md#2-you-cannot-just-turn-it-down).

**3. The null result.**
`VOICE_LLM_MODEL=llama3.2:1b make trace` (needs `ollama pull llama3.2:1b`)
A model a third the size, and end to end barely moves: the `llm` row shrinks
while the total holds. The model was never the biggest line item — which is
the thesis of the talk in one command.

---

*Next: [Part 2 · What to notice →](2-what-to-notice.md)*
