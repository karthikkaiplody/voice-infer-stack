# Live mode

*[← README](../README.md) · [← How it works](how-it-works.md)*

Talking to the agent, what the page does while you do, and what to check when
nothing happens.

## Run it

```bash
make setup          # dependencies and model weights, once
make live           # then open http://127.0.0.1:8080 and press Start listening
```

You need [Ollama](https://ollama.com) running, a microphone, and headphones.
`make setup` downloads the models (several GB; the language model alone is about
2 GB). Wait for the agent to greet you, then ask it something: *"what time do you
close on Sunday?"*, then *"tell me a pirate joke"*, and watch it decline.

No microphone but you have the models? The same pipeline runs from a WAV file, at
real 20 ms cadence: `make trace`.

**Wear headphones.** Microphone and speakers on one machine with no acoustic
echo cancellation means the agent hears itself: its own voice trips voice
activity detection and its own words come back through speech-to-text as your
next question. The pipeline mutes the microphone while the bot speaks, which
stops the loop, but headphones remove the problem rather than managing it.

## The page

One page, one address: http://127.0.0.1:8080. A switch in the header moves it
between recorded turns and your live agent, and the page starts fresh each time
you switch. `make demo` opens on recorded turns and `make live` opens on the live
agent. They start the same server with the same settings.

Switching to live for the first time takes a few seconds while the agent's code
loads. Switching is refused while the agent is listening, so the microphone is
never left running behind a page that no longer shows it.

**Tuning** (live only): six latency settings you can change while the agent is
stopped: the VAD silence window, the speech timeout, the STT safety timer,
waiting for the transcript, the smart-turn model, and a reply length cap. Each
has limits and a warning where a value is likely to hurt. Changes apply the next
time you press Start listening, and the page shows what each one launched as.

## If it greets you and then never answers

The microphone is the first suspect, not the pipeline. The greeting is the agent
talking; it says nothing about whether anything is being heard. When the input is
*exactly* silent for a couple of seconds, the page says so. The usual cause is a
system default input that is a virtual device (a mixer or loopback) with nothing
routed to it. `make devices` lists the inputs; pick the real microphone by its
number:

```bash
make devices                       # find the microphone's number
VOICE_AUDIO_DEVICE=3 make live
```

A microphone that is heard but never crosses the voice-activity gate
(`VOICE_VAD_MIN_VOLUME`, default 0.3) looks the same from the page and is not
detected: try a lower value.

## The library agent

`make live` runs an agent whose notes are a small library's opening hours, loans
and fines. It says hello when you start, looks up its notes before each answer,
and answers only from them. The notes live in [`agents/library/`](../agents/library/),
and [`agents/README.md`](../agents/README.md) shows how to write your own agent.
