# Instructions for AI agents

This workspace is for you to execute on a given task as a player on AzerothCore
using agent-wow.

Useful resources to reference:
- agent-wow: https://github.com/agent-wow/agent-wow
- azerothcore: https://github.com/azerothcore/azerothcore-wotlk
- wow client-data: https://github.com/wowgaming/client-data/releases
- agent-wow module template: https://github.com/agent-wow/go-module-template

## System checks

Before executing on a task, you should do a quick system check to ensure you have
all the requirements to play. If any of these fail, do not continue and suggest
debugging tips.

### 1. Dependencies

- Linux
- AzerothCore servers
- Go 1.27.1 or newer
- Docker and Docker Compose
- `protoc` CLI
- `agent-wow` CLI

### 2. Auth

Run `agent-wow auth status` to check if your auth session is valid. If not, run
`agent-wow auth login` and recheck again.

## Gameplay sessions

Before running `agent-wow char play ...`, check if the worldrpc port is in use since
there may be concurrent agents at play. If so, assign a different port with the environment
variable `AGENT_WOW_WORLDRPC_PORT`.

## Workspace directories

Any assets used or generated for your task must be kept within this workspace. That
includes configuration files, data, modules, scripts, etc. Some directories are available
for you to use.

- `./module`: agent-wow modules you build.
- `./scripts`: Scripts you use to interact with the gameplay session.
- `./journal`: Relevant information and findings that can be useful in future sessions.
- `./logs`: Any log outputs for debugging.
- `./.sandbox`: Not relevant for gameplay. Do not run, edit, or save anything here.

## Required task journal

Maintain `./journal/events.jsonl` for every task. Use JSON Lines: one complete JSON
object per line, with no Markdown or surrounding array. Append and flush records
as events happen; do not overwrite existing entries or wait until the task ends.

Every record must include:

- `timestamp`: the actual UTC time in ISO 8601 format, such as
  `2026-10-20T20:00:00.000Z`.
- `run_id`: a unique string chosen when work begins and reused for this task's
  records, so positions and timing can be matched to the same run.
- `event`: `task_started`, `position`, or `task_finished`.
- `character`: the character's name, or `null` if no character has been selected.

Record these events:

- **Task start:** append `task_started` immediately when you begin work, before
  system checks, research, or gameplay.
- **Character positions:** append `position` immediately after entering the world,
  at least once every five seconds while a gameplay session is active, and after
  a map change. Collect these automatically in the gameplay module or control
  script. Include `map_id` (integer) and `x`, `y`, `z` (numbers in the game's world
  coordinate system). Record the character's observed position from the game
  client or API, not a planned destination or an invented coordinate. If multiple
  characters are played, record each one's positions under its own name.
- **Task finish:** append `task_finished` before your final response, including
  when you stop because of a blocker or failure. Add `outcome`, one of `completed`,
  `blocked`, `failed`, or `interrupted`, and `elapsed_seconds`, a number calculated
  from this run's start and finish times. Use `completed` only when the task is
  complete. If you receive a stop request and can still write, record
  `interrupted` before exiting.

Example format (illustrative values only; write actual observations and times):

```jsonl
{"timestamp":"2026-10-20T20:00:00.000Z","run_id":"example-run","event":"task_started","character":null}
{"timestamp":"2026-10-20T20:05:00.000Z","run_id":"example-run","event":"position","character":"ExampleOrc","map_id":1,"x":123.4,"y":456.7,"z":10.2}
{"timestamp":"2026-10-20T20:30:00.000Z","run_id":"example-run","event":"task_finished","character":"ExampleOrc","outcome":"completed","elapsed_seconds":1800.0}
```
