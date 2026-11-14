# First-user check

Use a new test project and synthetic decisions. No mailbox, calendar or private
work material is needed. This is a small reproducible usability check, not a
benchmark claiming measured improvement.

1. Install from the public source and run `hive demo`. Note whether the purpose
   of the tool and its local storage are clear.
2. Run `hive init`, select a test project and connect your chosen host. Follow its
   hook approval step. Start a fresh session in the project and check that a
   HiveMind session ID is offered.
3. Ask the agent to save this explicitly synthetic decision: “Use SQLite for the
   local prototype.” Have it attach the source and reason through the documented
   session draft/close flow. Start a second session and ask which storage was
   chosen and why. Check both the answer and the offered HiveMind context.
4. In `hive memory`, correct the decision to “Use a JSON file for the local
   prototype”, inspect the preview and confirm. Ask again in a fresh session.
   Archive and undo a note to check that the controls are understandable.

Record only non-sensitive observations:

| Observation | Result |
| --- | --- |
| OS, Python, HiveMind version and agent host/version | |
| Time from installation to first offered memory | |
| Was the right decision offered with its source? | |
| Did the agent actually use the offered decision? | |
| Did the correction appear in the fresh session? | |
| What had to be explained again? | |
| First confusing instruction or failed command | |

Use the [issue tracker](https://github.com/Kevxn97/hivemind-public/issues) for a
redacted report. Do not attach your vault, journal, credentials, emails or real
session transcripts. The synthetic demo report is intended for reproducible
local checking; review local paths before sharing it.

Testing with external people remains a release validation task. A passing local
suite is not evidence that three new users can finish setup in ten minutes.
