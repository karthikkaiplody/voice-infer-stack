"""A local voice agent you can watch, stage by stage, while it answers you.

Where to look (each folder has its own docstring with the details):

    pipeline/    the voice agent itself: microphone -> speech-to-text -> model -> speech
    knowledge/   what the agent knows: agent definitions, notes, and the lookup step
    telemetry/   how a turn is measured: the event contract, tracing, live events
    server/      the local web server behind the browser UI, and the live tuning panel
    analysis/    read recorded turns: the latency budget and the HTML waterfall
    tools/       one-off scripts: record your voice, regenerate the test recordings

`config.py` holds every setting. `paths.py` holds every folder and file location.
This file is deliberately empty of imports: importing the package must stay cheap.
"""
