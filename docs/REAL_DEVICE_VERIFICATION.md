# Supervised real-device verification (a plan for YOU to run)

Nothing in this document is run by tests or by the coding assistant. You decide when, and whether, to connect the phone. Unit tests and
the simulated GUI suites (`pytest tests`, `python tools\run_isolated.py --all`) prove the logic; only the steps below can show how the
phone and a game behave. Record what you observe in the results template at the end, and do not call something "verified on hardware" unless
you saw it on the phone.

Labels used below: **[READ-ONLY]** looks only. **[STATE-CHANGING]** changes the phone, the PC or a file. **[APPROVAL]** you must decide
first: every ADB command that contacts a device, and everything that sends input to or changes the phone, is [APPROVAL]. **[PHONE]** you do it
by hand on the phone, with no PC involved. **[PC]** touches only the PC.

---------------------------------------------------------------------------------------------------------------------------------------

## What the engine does with delayed and cancelled actions (the source of truth for Stage E)

Taken from `app/input/engine.py` (module docstring, `_activate`, `key_event`, `release_all`) and `app/ui/main_window.py`. The checklist
below tests exactly this, nothing else.

**Timing.** A binding is *activated* when a key (or mouse button) that starts it is pressed and none of its other triggers is already held.
The first action starts at once. Every other action starts `delay_ms` after the ACTIVATION (the key press), never after a release and never after
another action ends. Actions due at the same instant start in list order.

| Kind of action | Modes | A delayed one... | Released early (the key goes up before the delay is over) | Released while running |
|---|---|---|---|---|
| **Held** | hold, repeat, drag | starts after its delay only if a trigger that started the binding is still held | **cancelled: it never starts** | stops (fingers lifted) when the LAST trigger that sustains it is released; another trigger still held keeps it going |
| **One-shot** | tap, double, swipe, pinch, sequence | starts after its delay **even if the key is already up** | **not cancelled: it still fires** | runs to its end on its own |
| **Toggle** | toggle | turns ON after its delay **even if the key is already up** | **not cancelled: it still turns on** | stays on after the key is released |

More rules that matter when you test:
* A toggle that is on is switched OFF by the next *fresh* activation (a press while none of the binding's triggers is held). A press while
  another trigger of the same binding is held JOINS the activation instead (no second finger, no flip).
* A press while an action of the same binding is still pending or running leaves that action alone (a one-shot is never started twice at once,
  a pending delayed action is neither doubled nor cancelled). Once it has finished, the next press starts it again.
* A mouse-wheel notch is an impulse and never sustains anything: hold and repeat run as a tap, drag runs as a swipe.
* A failure to start an action (no free finger, no connection) follows the binding's setting: "continue" lets its other actions go on,
  "abort" cancels all of that binding's actions. Not exercised on a phone in this plan (it needs all ten fingers busy).

**What cancels everything.** These are different from a key release. They all end in `engine.release_all()`: every delayed action still
waiting is dropped, every finger is lifted, every toggle goes off, the scope latch, stick, auto-run and look finger are cleared.
* Mapping is turned off: Esc, F9 or the Stop button, starting Edit controls, a smart-mode menu pause, a pause because the phone turned to an
  orientation the profile was not made for, and the connection ending (a disconnect, a failure, "Apply now").
* The app window loses focus (`changeEvent`): mapping stays ON, but fingers are lifted and everything pending is dropped. Keys that were held
  when focus left do nothing until they are pressed again.
* The phone's screen size or orientation changes (`screen_changed`): everything is dropped; keys still held do nothing until pressed again.
* The profile is edited or changed while mapping is on (`refresh_profile`).
After any of these, a delayed action that had not started yet NEVER starts, even though its key may still be held or was released a moment ago.
A mapping that was paused only because of a rotation starts again by itself when the phone is back in the right orientation; a toggle that was
on stays off.

---------------------------------------------------------------------------------------------------------------------------------------

## What is known and unknown about the earlier accidental sessions

* **Confirmed by logs and code** (the log records the time of day only, no dates): six real mirroring sessions at 01:01:17, 01:01:37, 01:01:46
  (step-7 verification) and 01:26:42, 01:27:34, 01:27:45 (step 8), by `intercept_test`, `editor_test` and `profile_ui_test`, which then still
  connected to a real phone if one was attached. Each pushed the scrcpy server and started it with `stay_awake=true` (from `settings.ini`) and
  `cleanup=true`. At 01:27:36 and 01:27:47 the phone's foreground app was Call of Duty Mobile and the app auto-loaded `COD- BR`; the saved
  `profile` setting changed from `BATTLE PRIME` to `COD- BR` as a result.
* **Reported, not independently established:** that no touch reached the phone (the tests replaced `control.send` right after connecting);
  that scrcpy's cleanup restored the stay-awake setting.
* **Unknown:** the phone's stay-awake and screen state, whether a forward or server process is left, the earlier `settings.ini` (apart from the
  `profile` value above), and which profile you want to open at start-up. Choose that profile yourself in the app. The copy
  `backups/settings.ini.before-step9` is the state at the start of the audit, not proven to be the original.
* The test tools can no longer do this: see `docs/TESTING.md`.

---------------------------------------------------------------------------------------------------------------------------------------

## Stage A: Preflight (nothing connects)
- **A1 [PC]** Close the app and other ADB users (Android Studio, other mirroring tools).
- **A2 [PC][STATE-CHANGING]** Copy the whole `%LOCALAPPDATA%\MobileKeyOne` folder (profiles, backups, `settings.ini`, logs) to a dated folder
  outside it and note the sha256 of `settings.ini`. This is a rollback to today's state, not to the original.
- **A3 [PC]** Do not test with a profile that is linked to a game (it auto-loads when that game opens) or with `Default` (it holds a hold-LMB
  mapping). Make a new profile from the **Empty** template (for example `zz-verify`) and review it in the editor before any mapping.
  Do not use `tools\make_*_profile.py` for this; if you ever use them, give `--output-dir` (see `docs/TESTING.md`).
- **A4 [PC][STATE-CHANGING]** In the app's Settings, before connecting: turn off "Load the profile automatically when its game opens"; leave
  "...and turn key mapping on" off; turn off "Mouse works like a finger" for the passive stages (it is on, and clicking the picture would touch
  the phone); decide on "Keep the phone awake" (it is on); leave Smart mode off.
- **A5 [PHONE]** Charge and unlock. Write down the current Stay awake and screen-timeout values. Open a harmless app, no game. Developer
  options > "Pointer location" shows finger traces and is the easiest way to see touches [STATE-CHANGING: switch it off afterwards].
- **A6 [PC][READ-ONLY, optional]** `pytest tests` and `python tools\run_isolated.py --all`: they never reach a phone.

**STOP 1.** Review A1 to A6. Continue only if you are comfortable.

## Stage B: Read-only look at the leftover state
- **B1 [PHONE][READ-ONLY]** Look at Stay awake, screen timeout and the USB debugging authorization; compare with your A5 notes.
- **B2 [PC][READ-ONLY]** In Task Manager, see whether `adb.exe` is running (a resident ADB server is normal).
- **B3 [APPROVAL][READ-ONLY, contacts the device]** Only if you choose to:
  `vendor\adb\adb.exe devices`; `vendor\adb\adb.exe forward --list` (expect nothing); `vendor\adb\adb.exe shell settings get global
  stay_on_while_plugged_in` (0 is normal, 7 means the server did not restore it; compare with B1, your own value may differ);
  `vendor\adb\adb.exe shell ls -l /data/local/tmp/scrcpy-server.jar` (the file existing is normal); `vendor\adb\adb.exe shell ps -A` (look for a
  leftover `app_process`).
- **B4 [APPROVAL][STATE-CHANGING, separate decision]** Any cleanup (`forward --remove-all`, `settings put ...`, deleting the jar) is yours to decide
  after reviewing the results. Nothing here does it for you, and a leftover is not proof it came from the accidental sessions.

**STOP 2.** Record the results and decide whether to go on.

## Stage C: Passive USB connection and mirroring (all [APPROVAL])
- **C1 [STATE-CHANGING]** Start the app (`run.bat`). It starts the ADB server and lists the phone ("Phone found"). Nothing else happens.
- **C2** Connect the cable and approve the USB debugging prompt on the phone.
- **C3 [STATE-CHANGING]** Press **Connect**. Expect log lines "Pushing scrcpy server", "Connected to '<model>'", "Video size ...".
- **C4 [READ-ONLY]** Watch 2 to 3 minutes with the mouse off the picture and the keyboard idle. Do NOT press Back, Home, Recents, "Phone screen off",
  "Restart the phone connection" or the colour check (each of them acts on the phone or on ADB).
- **C5 [STATE-CHANGING]** Press Disconnect, then close the app.
- **C6 [APPROVAL][READ-ONLY]** Repeat B1 and B3. With before and after values, this is the first real evidence of how cleanup behaves.

**STOP 3.** Continue only if mirroring was normal and the cleanup looks right.

## Stage D: Basic input (all [APPROVAL], all [STATE-CHANGING]: they send touches)
Set-up: your new profile, no game open, Pointer location on (optionally "Show touches on the phone" in Settings). Add each control in Edit controls on an
empty area; editing alone sends nothing.
**Stopping:** Esc (stops mapping and lifts everything), F9, F8 or `~` or holding Alt (free the mouse), then Disconnect, then close the app, and as a
last resort unplug the cable. **An unreleased touch** looks like a dot or trace that stays after the key is up, a "show touches" circle that
stays, or a marker that stays green.

| Step | Do | Expect |
|---|---|---|
| D1 | A tap control; press its key once | One touch down and up; nothing left on Pointer location |
| D2 | A hold control; hold 3 s, release | The finger stays down, then lifts |
| D3 | Two holds at once, then a tap while holding; release in a different order | Independent fingers; all lifted at the end |
| D4 | A WASD stick on an empty area; hold keys, release all | The finger leaves the centre and returns |
| D5 | A look area; lock the mouse (F8); move; free it | The finger drags, lifts when idle; the cursor comes back |
| D6 | "Any other key": click a spot, press a key or a combination; press Esc during a capture | Capture sends no touch; Esc cancels |

**STOP 4.** Continue only if every finger was released each time and the log has no "could not run" warnings.

## Stage E: Advanced bindings, one feature at a time (all [APPROVAL], [STATE-CHANGING])
Use the same non-game profile. The expected results come from the table at the top of this document.

**E1 Multiple triggers.** One control with E, F, the middle mouse button and Shift+G. Expect: any trigger starts one finger; a second trigger
pressed while the first is held adds no finger; the finger lifts only when the last trigger is released.

**E2 Multiple actions and delays** (one control, several actions; do each case on its own):
1. *Held, released early:* a hold with delay 600 ms. Tap the key and let go in under 600 ms. Expect: the hold never appears.
2. *Held, kept:* the same control; hold the key for 2 s. Expect: the hold starts at about 600 ms and lifts when you release.
3. *One-shot, released early:* a tap with delay 600 ms. Tap and release at once. Expect: the delayed tap STILL fires at about 600 ms.
4. *Toggle, released early:* a toggle with delay 600 ms. Tap and release at once. Expect: it turns on at about 600 ms and stays on; press again
   (after it is on): it turns off; press during the delay: nothing is doubled or cancelled.
5. *Order:* three actions with delays 0, 300 and 600 ms. Expect: they appear in that order, each from the moment of the press.

**E3 Explicitly shared trigger.** Two controls, a tap and a hold, shared on one key (Options > Keys > "Share this key"). Expect: both run, each with
its own finger. Without sharing, only the later control runs. The sharing survives restarting the app.

**E4 Independent hold and toggle.** A hold and a toggle sharing a key. Expect: they behave independently. Esc clears both and the toggle is off
afterwards.

**E5 What cancels vs what does not.** With a delayed action pending (use a long delay, for example 3 s) and then again with a hold and a toggle active:
1. Press Esc. Expect: nothing pending ever fires; every finger lifts; the toggle is off.
2. Turn mapping off with F9, then on. Expect the same.
3. Click another window (focus loss). Expect: fingers lift and the pending action does not fire; mapping stays on; the key you were holding does
   nothing until pressed again.
4. Release the key only (the contrast): held actions end, one-shots and toggles carry on (see the table).

**E6 Disconnect and reconnect.** With a hold active: press Disconnect (graceful) and look at the phone. Then, only if you are comfortable, unplug the
cable on purpose and reconnect. Record what you see on the phone and in the app (auto-reconnect is on in your settings); do not assume the result.

**E7 Rotation and resolution.** Use a profile made for one orientation. Rotate the phone with a hold active. Expect: fingers lift at the new size,
everything pending is dropped, mapping pauses if the orientation does not match and resumes by itself when the phone turns back. Then change the
size setting and press "Apply now" (a reconnect) and record what happens.

**STOP 5.** Live gameplay is outside this plan. If you choose it later: an offline or training mode, a spare account, and the game's rules first.

## Stage F: Results
Record each step in the template. A result is "verified on hardware" only if you observed it on the phone.

```
Date/time (match to app.log time of day): ____   App version: ____
Phone model: ____   Android version: ____   Cable/port: ____
Profile name + how it was reviewed before use: ____
settings.ini sha256 before / after: ____ / ____
Stage / step: ____
What I did: ____
Expected: ____
Observed: ____
Log lines / errors (copy): ____
Phone state before / after (stay awake, timeout, forwards): ____ / ____
Verified on physical hardware?  yes / no / partly     Evidence: ____
Anything unexpected on the phone or PC? ____
Decision at the STOP: continue / pause / abort
```

---------------------------------------------------------------------------------------------------------------------------------------

## What needs your explicit approval first
* Any ADB command that contacts a device, including the "read-only" ones in B3 and C6.
* Plugging in the phone, approving USB debugging, pressing Connect or Disconnect.
* Any key, mouse or touch input that goes to the phone, and the buttons Back, Home, Recents, "Phone screen off", "Restart the phone connection" and
  the colour check.
* Changing an Android setting (Pointer location, Show touches, Stay awake) or any cleanup on the phone.
* Changing `settings.ini` or a profile, choosing your start-up profile, or writing profiles with a generator script.
* Running any tool in `tools/` that needs `--really-use-real-phone`.
* Unplugging the cable on purpose, and anything involving live gameplay.
