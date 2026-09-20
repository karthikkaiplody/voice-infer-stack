"""The voice agent: audio in, a spoken answer out.

    agent.py          builds the Pipecat pipeline and runs it (a WAV file, or live)
    factory.py        creates each stage: speech-to-text, model, text-to-speech, VAD
    wav_transport.py  feeds a recorded WAV through the pipeline instead of a microphone
    warm.py           loads the models once so the first real turn is not slow
"""
