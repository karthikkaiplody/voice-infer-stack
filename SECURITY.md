# Security policy

## How this project is meant to behave

It is a local tool. These are the properties a security report can hold it to:

- **Loopback only.** The server listens on `127.0.0.1` and refuses any request
  whose `Host` or `Origin` is not local, on every route.
- **Metadata only.** Audio, transcripts, prompts, generated text, TTS text, tool
  arguments and results, raw errors, credentials, device names, hostnames,
  usernames and local paths are never written to the trace file, sent to the
  browser, or printed to the console at its default log level.
- **Your audio, words and traces stay on the machine.** This project sends them
  nowhere. Models are downloaded over the network on first setup, and the
  libraries that fetch them may contact their own servers to do so.
- **No secrets are needed.** There are no API keys, accounts or tokens.

## Reporting a vulnerability

Please **do not open a public issue** for a security or privacy problem.

Use GitHub's private vulnerability reporting: on the repository page, open the
**Security** tab and choose **Report a vulnerability**. Include what you did, what
you expected, what happened, and your macOS version. Please do not include real
conversations, recordings or personal data in the report; a synthetic example is
enough.

This is a small teaching project with one maintainer, so there is no response-time
guarantee. Reports are read, and confirmed problems are fixed and credited unless
you ask not to be.

## What counts

- Any way for a web page or another machine to reach the local server, including a
  Host or Origin check that can be bypassed.
- Any path by which raw content or machine details reach a trace file, the browser
  stream, the console, or a committed file.
- Path traversal through a scenario name, an agent name, or the files an agent
  loads.
- A secret, token or personal detail committed to the repository or its history.
- A vulnerable dependency that is reachable from the code here.

## What does not

- Anything that needs you to already control the machine and the same user account.
- The quality or safety of what a language model says.
- Platforms other than macOS on Apple Silicon.
- Findings in the third-party models themselves; report those upstream.
