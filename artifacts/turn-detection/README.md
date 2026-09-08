# Turn-detection false endpoint: captured evidence

Recorded 2026-09-05 during the Phase 0 spike, pipecat 1.8.1, M4 Pro / 24GB.

## What this shows

The agent answered **before the user finished speaking**, produced a throwaway
reply, then answered again with the real transcript. Every run of the fixture
did it. `turn.was_interrupted` is `true` on the turn span.

This is a false endpoint: `LocalSmartTurnAnalyzerV3` plus Silero VAD decided the
turn was over during a mid-utterance pause. Worth stressing for the talk: the
input here is **synthetic Kokoro speech with clean endpoints and no breath or
hesitation**. This is the easy case. Human speech is harder.

## Cost of a false endpoint

It is not just a wrong answer. The pipeline pays for a full LLM generation and
a TTS start that get thrown away, while the user is still talking. On a busy
system that is wasted capacity on every affected turn.

## The runs

Times are milliseconds relative to the start of each conversation span.


### Run 0 (warmup)
```
span             start     end     dur  detail
conversation         0    8786    8786  
turn                 0    8783    8782  interrupted=True
stt                505    2117    1612  transcript='Can you tell me?'
stt               1922    4316    2393  transcript='the weather is going to be like tomorrow afternoon.'
llm               3783    4347     564  out="I'd"
tts               4347    5010     663  chars=3
llm               5148    5745     598  out="I'm not aware of your current location's weather forecast. I"
tts               5388    6780    1392  chars=182
```

### Run 1
```
span             start     end     dur  detail
conversation         0    8803    8803  
turn                 0    8801    8801  interrupted=True
stt                504    1446     942  transcript='Can you tell me?'
stt               1252    4400    3148  transcript='what the weather is going to be like tomorrow afternoon.'
llm               3803    4411     608  out="I'd be happy to help answer your question"
tts               4412    4414       2  chars=41
llm               5214    5752     538  out="I'm not aware of your current location's weather forecast. C"
tts               5463    6710    1247  chars=157
```

### Run 2
```
span             start     end     dur  detail
conversation         0    8806    8806  
turn                 0    8803    8803  interrupted=True
stt                503    1461     958  transcript='Can you tell me?'
stt               1267    4388    3121  transcript='what the weather is going to be like tomorrow afternoon.'
llm               3795    4404     610  out="I'd be happy to help answer your question."
tts               4405    4407       2  chars=42
llm               5206    5774     569  out="I'm not aware of your current location's weather forecast. C"
tts               5482    6713    1231  chars=157
```

### Run 3
```
span             start     end     dur  detail
conversation         0    8810    8810  
turn                 0    8806    8806  interrupted=True
stt                503    1444     941  transcript='Can you tell me?'
stt               1253    4369    3117  transcript='what the weather is going to be like tomorrow afternoon.'
llm               3782    4378     596  out="I'd be happy to help answer"
tts               4379    4381       2  chars=27
llm               5181    5730     549  out="I'm not aware of your current location's weather forecast. C"
tts               5441    6686    1246  chars=157
```

## What was changed afterward

`filter_incomplete_user_turns=True` on `LLMUserAggregatorParams`, so the
benchmark measures one clean turn. The flag is set in `config.py` and named in
the README, because a flag that silently changes headline latency numbers is
exactly the kind of thing a reader should not have to discover for themselves.

Phase 2 builds the turn-detection case studies starting from this trace.

