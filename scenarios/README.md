# Conversation regression scenarios

Self-contained behavior checks for Ledger's conversational reasoning. Each scenario scripts
a prior conversation, sends one message, and states what a *good* response looks like, so
you can re-run it after any prompt or model change and judge whether behavior held.

These are **judgment fixtures, not pass/fail asserts.** The model is stochastic and the
"expect" note is a rubric for a human (or a reviewing agent) to grade against. Read the
`<think>` block and the reply, not just an exit code.

## Run one

```
.venv\Scripts\python.exe ledger_cli.py simulate --scenario scenarios/iron_ferritin_contradiction.json
```

Writes nothing to the DB. Prints the `Expect:` rubric, the tool calls, the `<think>` block,
the visible reply, and token cost. Override the model to compare:

```
... simulate --scenario scenarios/iron_ferritin_contradiction.json --model gemini-2.5-pro
```

## File format

```json
{
  "name":    "short_slug",
  "expect":  "What a good answer does, and what counts as a FAILURE.",
  "message": "the user's message for this turn",
  "history": [
    {"role": "user",  "content": "..."},
    {"role": "model", "content": "..."}
  ]
}
```
`history` roles are Gemini's `user` / `model` (not `assistant`), oldest first. Omit `history`
to run against live chat history; omit `message` only if you pass text on the command line.

## Scenarios

| File | Checks |
|---|---|
| `iron_ferritin_contradiction.json` | Does Ledger *abandon* a falsified mental model when the user presents contradicting data, instead of rescuing it? (The core sensemaking behavior.) |
