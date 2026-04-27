# Voice Agent Prompt Design Notes

## Purpose

This document explains the reasoning behind `prompt-v2.md`.
It captures the structure, techniques, and design decisions used for a fully voice-based agent prompt.

## Why Voice Prompting Needs A Different Approach

- Voice is linear. The user cannot scan, reread, or jump back.
- TTS reads exactly what it gets. Formatting, long clauses, and awkward punctuation sound unnatural.
- STT is imperfect. A good voice prompt must expect partial, noisy, or ambiguous input.
- Turn-taking matters. If the agent keeps talking after asking a question, the interaction feels broken.
- Long responses increase perceived latency. Even if the model is fast, verbose answers make the agent feel slow.
- Spoken conversations need repair logic. Silence, "I don't know", short answers, and interruptions are normal.

## What A Good Voice Prompt Must Do

- Define a clear role and success condition.
- Tell the model how to sound, not just what to do.
- Constrain response length aggressively.
- Make the next-step logic explicit.
- Tell the model when to ask, when to wait, and when to move on.
- Include fallback handling for unclear or low-engagement responses.
- Make tool usage and end conditions explicit.

## Recommended Prompt Structure

The best current guidance for voice agents converges on a sectioned prompt.
Each section should do one job.

### Role and objective

- Who the agent is.
- Who the user is.
- What the conversation is trying to achieve.
- What "success" means.

Why:

- It anchors the model early.
- It reduces drift into generic assistant behavior.

### Tone and spoken style

- Warmth, professionalism, pacing, confidence, empathy.
- Audience calibration, such as B1 English by default.

Why:

- Voice quality is not just content. Tone is part of the product.
- This section stops the model from sounding too corporate, too formal, or too chatty.

### Spoken output rules

- Write for TTS.
- Keep replies short.
- Spell out numbers when needed.
- Avoid markdown and visual formatting.
- Use contractions.
- Limit punctuation to what helps speech.

Why:

- TTS exposes every formatting mistake.
- Voice prompts need stronger formatting rules than chat prompts.

### Conversation rules

- One question per turn.
- Acknowledge plus one question is allowed.
- Bridge each question to the user's previous answer.
- No multi-part questions.
- No filler acknowledgements.

Why:

- This is the core rule set that controls naturalness.
- Most voice failures come from stacked questions and generic responses.

### Silent state and decision logic

- What the model should track internally.
- How to classify the user.
- What conditions allow moving to the next phase.

Why:

- Voice agents still need state, but the state should be described as tracking rules, not chain-of-thought.
- Explicit state reduces missed topics and looping.

### Conversation flow

- Opening.
- Path selection.
- Phase goals.
- Transition rules.
- Closing.

Why:

- A voice agent needs a controlled path, but it should still sound conversational.
- State transitions are more reliable than long prose instructions.

### Error handling and recovery

- What to do with silence.
- What to do with "I don't know".
- What to do with unclear audio.
- What to do after repeated low-engagement turns.

Why:

- Recovery behavior should be centralized.
- If recovery logic is scattered, the model becomes inconsistent.

### Tool rules

- When to call tools.
- Whether to speak before or after calling them.
- When to end the session.

Why:

- Tool behavior changes the conversational feel.
- Voice systems need tool rules to be operational, not implied.

## Core Techniques Used In Prompt V2

### 1. One question per turn

- The prompt forces exactly one question per turn.
- The agent can use one short acknowledgement sentence before the question.

Why:

- This creates clean turn-taking.
- It reduces cognitive load.
- It improves barge-in handling because the agent is less likely to be mid-ramble.

### 2. Hard length constraints

- Most turns are limited to one or two short sentences.
- Most turns should stay under roughly thirty words.

Why:

- Voice responses need tighter bounds than text responses.
- This lowers perceived latency and improves comprehension.

### 3. Progressive disclosure

- The agent should reveal one idea at a time.
- It should not dump multiple concepts, examples, and follow-ups into one turn.

Why:

- Spoken information is easy to miss.
- Progressive disclosure feels more natural in a live conversation.

### 4. Bridge every question

- Every question must connect to the student's previous answer.
- If they mention a company, skill, tool, or feeling, the next question should use that detail.

Why:

- This is one of the strongest techniques for making an agent sound attentive instead of scripted.

### 5. Specific acknowledgements

- Avoid empty praise such as "Great" or "Nice".
- Reference a concrete detail before asking the next question.

Why:

- Specific acknowledgement sounds human.
- Generic praise sounds repetitive and fake.

### 6. Silent path classification

- The prompt tells the model to classify the user into conversational paths without mentioning internal reasoning.
- It avoids asking the model to expose or think about chain-of-thought explicitly.

Why:

- Modern voice prompting guidance prefers explicit decision rules over chain-of-thought instructions.
- This is cleaner, safer, and easier to tune.

### 7. Bounded follow-ups

- The prompt allows the agent to go deeper when the student mentions something specific.
- It also limits the agent to about two follow-ups on the same thread before moving on.

Why:

- This keeps the conversation adaptive without letting it stall.

### 8. Uncertainty-friendly collection

- The prompt treats "I don't know" and unclear answers as valid outcomes.
- The agent is instructed not to force certainty.

Why:

- The goal is signal collection, not interrogation.
- For this use case, uncertainty is useful information.

### 9. Language mirroring

- The prompt mirrors the user's language and code-mix.
- It preserves English technical terms.
- It uses the native script of the language being spoken when the TTS stack supports it.

Why:

- This improves comfort, comprehension, and TTS behavior.

Important note:

- Script choice is TTS-engine dependent.
- If the TTS model sounds worse with transliterated Tamil or Hindi, the prompt should prefer Tamil script for Tamil and Devanagari for Hindi.
- Roman transliteration should only be used if the speech stack handles native Indic scripts poorly.

### 10. Explicit recovery for real voice behavior

- The prompt handles short answers, repeated short answers, silence, repeated "I don't know", and unclear audio.

Why:

- These are normal in live calls and should not be treated as edge cases.

### 11. Explicit end conditions

- The prompt defines when to close, redirect, or end the session.

Why:

- Voice agents should not loop forever.
- Clear stop rules improve reliability.

## Key Design Decisions For This Agent

### Preserve the two-part interview shape

- Part 1 collects job plan.
- Part 2 collects job research awareness.

Why:

- The product goal is still correct.
- The improvement needed is not a new goal. It is better execution.

### Keep path-based behavior

- Path A for clear students.
- Path B for vague students.
- Path C for uncertain students.
- Special handling for hybrid, multiple-option, and non-job-plan answers.

Why:

- The current prompt already has a strong triage idea.
- This should be preserved, but expressed more operationally.

### Keep target, dream, and backup before research

- Except for uncertain students, the prompt keeps this ordering.

Why:

- It creates a stable structure.
- It gives later questions a concrete anchor.

### Add stronger repair before progression

- Short or disengaged answers should trigger a repair move before the agent advances.

Why:

- Moving on too quickly causes low-quality signal and a cold conversational feel.

### Make unclear audio explicit

- The prompt adds separate guidance for partial, noisy, or unintelligible audio.

Why:

- Voice prompting guidance consistently recommends this.
- Speech errors are different from low engagement and should be handled differently.

### Keep examples, but use them sparingly

- Prompt V2 uses examples as style anchors, not as a script.

Why:

- Models learn strongly from examples.
- Too many examples can make replies repetitive.

### Add variety instruction

- The prompt asks the model not to repeat the same acknowledgement pattern.

Why:

- Repetition is one of the most common ways voice agents sound robotic.

### Make tool usage more concrete

- Prompt V2 makes it clear when to call `start_question(id)`, `record_question_discussed(id)`, and `end_session()`.

Why:

- Tool usage needs to be visible in the prompt's operating logic.

## Common Mistakes To Avoid In Voice Prompts

- Long paragraphs of instructions instead of short sections and bullets.
- Telling the model to do chain-of-thought internally.
- Asking more than one question in a turn.
- Writing for text instead of TTS.
- Generic acknowledgements such as "Good" or "Nice".
- Moving to the next topic before repairing a weak answer.
- Mixing conversation goals, examples, and tool instructions together.
- Leaving end conditions vague.
- Forgetting language rules in multilingual settings.
- Forgetting that silence, noise, and interruptions are normal.

## How To Evaluate And Iterate

- Review real transcripts, not just the prompt text.
- Mark where the conversation felt robotic, confusing, repetitive, or too long.
- Tune the smallest section that causes the issue.
- Prefer small wording changes over full rewrites when testing.
- Track concrete metrics:
- success rate
- average turns to completion
- clarification rate
- repeated "I don't know" rate
- early-exit rate
- off-topic drift rate

## Prompt Review Checklist

- Is the role unambiguous?
- Is the voice style explicit?
- Are spoken-output rules clear?
- Is there a hard limit on response length?
- Is one-question-per-turn enforced?
- Does the prompt explain when to wait?
- Are path-selection rules concrete?
- Are recovery rules centralized?
- Are tools and end conditions explicit?
- Are there any conflicting instructions?

## Sources That Shaped These Decisions

- Vapi Prompting Guide
- OpenAI Voice Agents Guide
- OpenAI Realtime Prompting Guide
- Practitioner voice-agent guidance from the `voice-agents` reference shared during this session

These sources all point in the same direction:

- use sectioned prompts
- prefer bullets over long prose
- constrain voice response length hard
- define recovery behavior explicitly
- keep tool behavior concrete
- iterate from real conversation failures
