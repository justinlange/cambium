# Elliot: driving the real tree from your simulator

Your simulator already knows how to do this — the work landed in your app as
the `CambiumBridge` transport and the 📡 toggle. This page is everything you
need the first time, with zero terminal work on your part.

## What has to be true first (someone else's job, usually Justin)

1. A **bridge board** is plugged into a laptop at the tree, and
2. the **cambium daemon** is running on that laptop.

If you're told "cambium is up", you're good. Everything below is you.

## Your three steps

1. Open the simulator with the cambium address in the URL. On the tree
   laptop itself that is:

       http://localhost:5173/?cambium=ws://localhost:8600/ws

   (From another computer on the same WiFi, replace `localhost` in BOTH
   places with the tree laptop's address — ask whoever set it up.)

2. In Controls, click **📡 drive real** so it says **(armed)**.

3. Play patterns, shows, anything. The real lanterns follow whatever the
   twin renders. **BLACKOUT works on the real tree too.**

## The night switch

Real lanterns ignore shows during daytime — they think they should be
sleeping and charging. That's a safety feature, not a bug. If the tree
stays dark, press the **🌙 night** button in your Fleet panel (it rides
through the same connection). At an actual nighttime, none of this is
needed.

## What you'll see, and why it's normal

| You see | Why |
|---|---|
| Patterns look scrambled on the tree | Nobody has told the system where each lantern hangs yet. The camera mapping session fixes this; afterwards your patterns land exactly where the sim shows them. |
| Real lanterns dimmer than the sim | Each lantern protects its own battery and caps its brightness. Charge fixes it; nothing to configure. |
| Some lanterns never respond to patterns | A few are still on older radio firmware (they'll still flash white for camera mapping). They're on the list for an update. |
| Tree does its own thing ~3 s after you stop | By design: when your stream goes quiet, lanterns return to their autonomous show. Never blank, never stuck. Close the laptop and walk away safely. |
| The 📡 toggle flips itself back off | The daemon isn't reachable — wrong URL, or it isn't running. Ask whoever runs the tree laptop. |

## The one rule

You can't break the tree from the simulator. Every command your app sends
is a short-lived lease the lantern can walk away from, and each lantern's
own power protection always wins. Experiment freely.
