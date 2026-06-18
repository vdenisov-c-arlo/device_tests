---
name: devloop
description: Full development loop — from JIRA ticket to verified firmware on device. Iterates through research, code, build, flash, log analysis until success or escalation.
---

# DevLoop — End-to-End Development Cycle

## When to Use

When the user wants to take a task from description to working firmware on device, with automated verification. This skill orchestrates the full cycle:

```
Gather Requirements → Research → Plan → Code → Build → Flash → Verify → Done
       ↑                                                    │
       └────────────────── (retry if failed) ───────────────┘
                         (max 3 retries, then escalate)
```

## Phase 0: Gather Context

### 0.1 Prompt the user

Ask the user (via AskUserQuestion) for:

1. **JIRA ticket** (optional): e.g., "PEGA-1234" — if provided, fetch ticket details
2. **Task description**: What needs to be done — the desired behavior or bug to fix

If the user already provided both in their message, skip the prompt.

### 0.2 JIRA context (if ticket provided)

Create working directory and fetch ticket:
```
$ARLO_CLAUDE_SETTINGS/JIRA/<TICKET>/
```

If the JIRA MCP server is available, fetch the ticket:
```
mcp__atlassian__getJiraIssue or mcp__arlochat__jira_read_issue
```

Save ticket summary to the working directory. This becomes the JIRA context for `getlogs-lory` and `get-crashdump-lory` skills.

### 0.3 Check prerequisites

Before starting any work, verify:

```bash
# Serial mux running?
nc -z localhost 9001 2>/dev/null && echo "ISP mux: OK" || echo "ISP mux: NOT RUNNING"
nc -z localhost 9002 2>/dev/null && echo "MCU mux: OK" || echo "MCU mux: NOT RUNNING"
```

**If serial_mux is not running**, stop and ask the user:
```
serial_mux is not running. Please start it from your terminal:

  $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_terminals.sh

Then tell me to continue.
```

Wait for user confirmation before proceeding.

---

## Phase 1: Research & Planning

### 1.1 Analyze the problem

Based on the task description and any JIRA context:

- Search relevant code (use Explore agent or grep)
- Read related source files
- Check JIRA/INDEX.md for prior analysis of related tickets
- If a crash dump exists on device, pull it: `/get-crashdump-lory`
- If logs are relevant, pull them: `/getlogs-lory`

### 1.2 Create a plan

Decide the approach based on task complexity:

| Task Type | Approach |
|-----------|----------|
| Bug fix (single file, clear root cause) | Fix directly, skip heavy planning |
| Bug fix (unclear root cause) | Research → hypothesis → targeted fix |
| Small feature (< 3 files) | Plan inline, implement directly |
| Large feature (new module, multi-file) | Use `/new-feature` workflow with full agent team |

For direct implementation, create a brief plan (mental or in conversation) covering:
- Files to modify
- Expected behavior change
- How to verify on device

### 1.3 User approval

Present the plan to the user. Wait for approval before coding.

---

## Phase 2: Implementation

Based on the plan:

- **Bug fix / small change**: Implement directly using fw-developer patterns (Edit tool, write code)
- **Large feature**: Invoke `/new-feature` skill which orchestrates the full agent team

After implementation, verify the change compiles with a quick check if possible (e.g., syntax check, header inclusion).

---

## Phase 3: Build

Invoke the rebuild skill:

```bash
make lory-2k-refresh
```

Use `timeout: 1800000` (30 minutes).

**On build failure:**
- Read the last 50 lines of output
- Diagnose the error (missing include, type mismatch, linker error, etc.)
- Fix the code
- Retry build (counts as part of the same iteration, not a loop retry)

**On build success:**
- Extract firmware version from `output/lory-2k/target/etc/os-release`
- Verify `.enc` file exists in `output/lory-2k/images/`

---

## Phase 4: Flash

Invoke `/flash-lory` skill:

```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/flash_lory.py
```

Use `timeout: 600000` (10 minutes).

**On flash failure:**
- Check if device is reachable
- Check if serial_mux is still running
- Retry flash once
- If still failing, escalate to user

---

## Phase 5: Verify

### 5.1 Pull logs

Wait 10-30 seconds after flash (let device boot and run the scenario).

If the task requires specific triggering (e.g., "press button", "wait for motion"), inform the user what action is needed and wait for confirmation.

Then pull logs:
```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/get_logs.py "<JIRA_DIR>/logs_verify_<attempt>/"
```

### 5.2 Read logs from serial mux

Also read recent serial data directly:
```bash
$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux_client --section isp --cmd "" --timeout 5000
```

### 5.3 Analyze

Check logs against the expected behavior from the task description:

- **Success criteria met?** → Proceed to Done
- **Crash detected?** → Pull crash dump, analyze, go to retry
- **Wrong behavior?** → Analyze what went wrong, go to retry
- **Inconclusive?** → Ask user to trigger the scenario, re-check

### 5.4 Decision

| Outcome | Action |
|---------|--------|
| Verified working | → Phase 6 (Done) |
| Failed, attempt < 3 | → Phase 1 (Research, with new information from logs) |
| Failed, attempt >= 3 | → Escalate |

---

## Phase 6: Done

Report to user:
```
Task complete.
  JIRA: <ticket>
  Change: <brief description of what was modified>
  Files: <list of modified files>
  Firmware: <version>
  Verification: <what was checked and confirmed>

  Logs saved to: <path>
```

If on a feature branch, offer to create a PR: "Want me to create a PR with `/create-pr`?"

---

## Retry Logic

Each retry iteration:
1. Increment attempt counter (starts at 1)
2. Log what failed and why
3. Return to Phase 1 with **new information** (the failure logs, the wrong behavior observed)
4. Revise the plan based on what was learned
5. Implement the fix
6. Build → Flash → Verify again

**Critical rule**: Each retry must change something. If the same fix is attempted twice, escalate immediately rather than looping.

---

## Escalation (after 3 failed attempts)

Report to user:
```
ESCALATION: Unable to complete task after 3 attempts.

Task: <description>
JIRA: <ticket>

Attempts:
  1. <what was tried> → <what failed>
  2. <what was tried> → <what failed>
  3. <what was tried> → <what failed>

Current state:
  - Code changes: <list files modified>
  - Last build: <pass/fail>
  - Last flash: <pass/fail>
  - Logs: <path to logs from last attempt>

Possible next steps:
  - <suggestion 1>
  - <suggestion 2>
  - <suggestion 3>

Need human input to proceed.
```

---

## State Tracking

Track loop state using Tasks (TaskCreate/TaskUpdate):

```
Task: devloop/<ticket-or-name>
  Status: in_progress
  Metadata:
    attempt: 1
    phase: "build"
    jira: "PEGA-1234"
    description: "..."
    plan: "..."
    files_modified: [...]
```

Update task status as you move through phases.

---

## Summary of Skills Used

| Phase | Skill/Tool | Purpose |
|-------|-----------|---------|
| 0 | `mcp__atlassian__getJiraIssue` | Fetch JIRA ticket |
| 1 | Explore agent, grep, Read | Research code |
| 1 | `/get-crashdump-lory` | Pull existing crash dump |
| 1 | `/getlogs-lory` | Pull existing logs |
| 2 | Direct coding or `/new-feature` | Implement changes |
| 3 | `/rebuild` or `make lory-2k-refresh` | Build firmware |
| 4 | `/flash-lory` | Flash device |
| 5 | `/getlogs-lory`, `serial_mux_client` | Pull and read logs |
| 5 | `/get-crashdump-lory` | Pull crash dump if crash detected |
| 6 | `/create-pr` | Create pull request (if asked) |

---

## Example Invocation

User says:
```
/devloop
```

Agent prompts:
```
JIRA ticket (optional): ___
Task description: ___
```

Or user provides context directly:
```
/devloop PEGA-1234 Fix the LED not turning off after motion event ends
```
