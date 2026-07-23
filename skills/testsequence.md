# Test Sequence

## When to Use

When the user wants to define, save, or run a repeatable test sequence on the device.

## Arguments

- If arguments are provided, treat them as a description of a new test sequence to save.
- If no arguments are provided (or argument is an existing alias), propose running one of the saved sequences.

## Steps

### If arguments describe a new test sequence:

1. **Parse the description** — understand what steps the test involves (button presses, log monitoring, timing, expected outcomes, etc.)

2. **Generate an alias** — derive a short kebab-case alias from the description (e.g., `pir-sensitivity-wake`, `sleep-wake-cycle`). If the user included an explicit alias, use that.

3. **Save the sequence** to `/home/denisov/arlo/claude_settings/test_sequences/<alias>.md` with this format:

   ```markdown
   # <Title>

   **Alias:** `<alias>`
   **Created:** <date>

   ## Description
   <user's description, clarified>

   ## Prerequisites
   - <what must be true before running: device state, firmware version, serial_mux, etc.>

   ## Steps
   1. <step>
   2. <step>
   ...

   ## Expected Result
   - <pass criteria>

   ## Pass/Fail Criteria
   - PASS: <condition>
   - FAIL: <condition>
   ```

4. **Confirm** — print the alias and summary to the user.

### If no arguments (or an existing alias):

1. **List saved sequences:**
   ```bash
   ls /home/denisov/arlo/claude_settings/test_sequences/*.md
   ```

2. **Present them** to the user with alias and one-line description.

3. **On user selection**, read the sequence file and execute the steps interactively:
   - For device commands: use serial_mux (ISP port 9001, MCU port 9002)
   - For button presses: use testbot4_do_pulse.py
   - For log monitoring: connect to appropriate serial port and watch for patterns
   - For timing: use appropriate delays
   - Report PASS/FAIL based on criteria in the sequence file.

## Execution Notes

- Always connect to serial_mux ports via TCP (`localhost:9001` for ISP, `localhost:9002` for MCU)
- **Connect and start recording logs BEFORE triggering any action** (button press, reset, etc.) — crashdumps and early boot messages are lost if you connect after the event
- Save logs for every cycle (pass or fail) so post-mortem analysis is always possible
- Use `/home/denisov/arlo/claude_settings/utils/custom/device_tests/testbot4_do_pulse.py` for button presses
- When monitoring logs, use a timeout to avoid hanging forever
- Print real-time output so the user can see what's happening
- At the end, clearly state PASS or FAIL with evidence

## Error Handling

| Error | Fix |
|-------|-----|
| serial_mux not running | Ask user to start it |
| Testbot4 unreachable | Report and abort |
| Timeout waiting for expected log | Report FAIL with what was seen |
| Device unresponsive | Try wake via SYNC, if still dead report FAIL |
