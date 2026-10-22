# Smooth picture (generated in-between pictures)

What it is: with **Smooth picture** on (Settings > Details, or the **F5** hotkey), the PC shows a picture *halfway* between every two
pictures the phone sends, then the real one half a picture later. A 30-picture/s stream is shown as 60, a 45 as 90.

What it is **not**: it does not make the game run at more FPS (the phone does that), it is not AMD FSR / AFMF (those need the game or an
AMD driver) and it does not change anything that is sent to the phone. Taps and swipes are unaffected.

## The price
Every real picture is shown **half a picture later** than without smoothing: about 17 ms on a 30/s stream, 11 ms on 45/s, 8 ms on 60/s.
For aiming and shooting, leave it off or use the highest stream rate your phone and cable can sustain (the **Max** preset);
for looking around, looting or watching it is a pure gain. Use F5 to switch while playing.

## How the picture is made (all on the graphics chip, `app/video/widget.py`)
1. *Motion*: for every 16x16 block, a coarse-to-fine search (blurred copies of the pictures first, then the detail) finds where it went
   between the previous and the newest real picture. Up to about +/-127 px per picture, in quarter pixels.
2. *Picture*: for every pixel the motion of its block (or of a neighbouring block, or "none" for something that did not move, such as the
   game's interface) is checked on the pixel's own surroundings, brightness AND colour. The previous picture moved forward and the next
   one moved back are averaged. Colour conversion, brightness/contrast/saturation and sharpness are applied exactly as for real pictures.
3. *No guessing*: where no motion explains the change (a scene cut, an explosion, a very fast turn) the nearest real picture is shown
   instead. The result is then the same as no smoothing, never a double image.

## When it pauses itself (the settings page says why)
* the stream is already about as fast as the screen can show (twice the stream rate would exceed the refresh rate);
* the stream is too slow or stalls;
* the graphics chip needs more than 70 % of the time between two pictures for one generated picture (measured now and then, twice in a row).
  Making a picture costs roughly 20-35 ms at 1920 px on an integrated Intel chip and several times less on a GTX 1650: if Windows runs
  the app on the integrated chip, give `python.exe` the high-performance GPU in *Windows Settings > Display > Graphics*.
* the graphics driver cannot compile the shaders (OpenGL 3.0 or newer is needed); the picture itself is then unaffected.

## Tested (simulated, on this PC's graphics chip: `tools/smooth_gl_test.py`; the Pacer in `tests/test_smooth_pacer.py`)
Textured scenes moved by known amounts (including half pixels and noise): the generated picture is within about 1-3 grey levels of the true
halfway picture, where plain blending is off by 12-18 and repeating a picture by 15-22. Still areas are reproduced exactly, a scene cut is
not blended, the real picture follows exactly, and off means off.

## Not verified
Quality on real game footage (menus, particles, fast turns, HUD over a moving world) has not been judged by eye: only synthetic scenes
were measured. Check it yourself in a training ground, and tell me where it looks wrong.
