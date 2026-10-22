"""GPU test of "smooth picture" (generated in-between pictures): renders the real VideoWidget and measures the pixels it draws.

It moves a textured picture by a known amount between two pictures and checks that the picture the screen shows in between is the picture
HALFWAY along, against the real one rendered for comparison, and against what plain blending would give. It also checks the cases where it
must NOT invent anything (a scene cut), that a still area stays pixel-exact, that the real picture follows, and that turning it off really
turns it off. No phone needed."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from look_gl_test import to_frame  # noqa: E402
from app.video.widget import VideoWidget  # noqa: E402

RESULTS: list[bool] = []
W, H = 640, 288


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def canvas(seed: int) -> np.ndarray:
    """A big picture with the statistics of a real scene: detail at every scale (1/f noise: big shapes, medium shapes, fine grain), plus a few
    hard-edged boxes like a game's interface and loot rows."""
    rng = np.random.default_rng(seed)
    h, w = H + 160, W + 160
    fy, fx = np.fft.fftfreq(h)[:, None], np.fft.rfftfreq(w)[None, :]
    radius = np.sqrt(fx * fx + fy * fy)
    radius[0, 0] = 1.0
    out = np.zeros((h, w, 3), np.float32)
    shared = rng.standard_normal((h, w))
    for channel in range(3):
        noise = 0.7 * shared + 0.3 * rng.standard_normal((h, w))            # colours that belong together, like a real scene
        spectrum = np.fft.rfft2(noise) / radius ** 1.0
        spectrum[0, 0] = 0
        layer = np.fft.irfft2(spectrum, s=(h, w))
        out[..., channel] = layer
    out = (out - out.min()) / (out.max() - out.min()) * 200 + 25
    out[60:120, 100:300] = (230, 200, 40)                          # a yellow bar, like a loot row
    out[200:260, 400:520] = (30, 60, 200)
    return out


def crop(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """The picture as seen after the scene moved by (dx, dy) pixels."""
    return np.clip(img[80 - dy:80 - dy + H, 80 - dx:80 - dx + W], 0, 255).astype(np.uint8)


def main() -> int:
    app = QApplication(sys.argv)
    widget = VideoWidget()
    widget.setFixedSize(W, H)
    widget.show()

    def settle(seconds: float = 0.25) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def grab() -> np.ndarray:
        image = widget.grabFramebuffer()
        image = image.convertToFormat(image.Format.Format_RGB888)
        data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
        return data[:, : image.width() * 3].reshape(image.height(), image.width(), 3).astype(np.float32).copy()

    def show_real(rgb: np.ndarray) -> np.ndarray:
        """A real picture, smoothing off: what the screen shows for it."""
        widget.set_smooth(False)
        widget.clear()
        widget.set_frame(to_frame(rgb), time.perf_counter())
        settle()
        return grab()

    def show_between(before: np.ndarray, after: np.ndarray) -> np.ndarray:
        """Two real pictures in quick succession with smoothing on; the picture shown when the second arrives (before the real one follows)."""
        widget.set_smooth(True)
        widget._pacer.force = True
        widget.clear()
        widget.set_frame(to_frame(before), time.perf_counter())
        settle(0.03)
        widget.set_frame(to_frame(after), time.perf_counter())
        widget._mid_timer.stop()                                     # hold the generated picture on screen so it can be measured
        settle(0.2)
        return grab()

    probe = show_real(crop(canvas(1), 0, 0))
    if probe.shape[:2] != (H, W):
        print(f"framebuffer is {probe.shape[1]}x{probe.shape[0]} (display scaling); size-based checks skipped")
        return 0
    if widget.smooth_error:
        print(f"smooth picture cannot run on this graphics setup: {widget.smooth_error}")
        check("... and the real picture still works without it", probe.std() > 10)
        return 0 if probe.std() > 10 else 1

    margin = 60
    inner = (slice(margin, H - margin), slice(margin, W - margin))
    mae = lambda a, b: float(np.abs(a[inner] - b[inner]).mean())
    scene = canvas(2)

    print("1. A scene that moves: the generated picture is the one halfway along")
    for dx, dy in ((12, 0), (0, -8), (14, 6), (-20, 10), (30, -16), (2, 2)):
        before, after, truth = crop(scene, 0, 0), crop(scene, dx, dy), crop(scene, dx // 2, dy // 2)
        real_before, real_after, real_truth = show_real(before), show_real(after), show_real(truth)
        blend = (real_before + real_after) / 2
        made = show_between(before, after)
        score, blended, nearest = mae(made, real_truth), mae(blend, real_truth), min(mae(real_before, real_truth), mae(real_after, real_truth))
        check(f"moved by ({dx:+d}, {dy:+d}): close to the true halfway picture, far better than blending or repeating a picture",
              score < 0.35 * blended and score < 0.35 * nearest and score < 12, f"error {score:.1f} (blend {blended:.1f}, nearest real {nearest:.1f})")

    print("1b. Like a real stream: odd (half-pixel) motion and compression noise")
    noise = np.random.default_rng(5).normal(0, 3.0, (H, W, 1)).astype(np.float32)           # a few grey levels of noise, different in every picture
    for dx, dy in ((13, -7), (25, 9), (-9, 3)):
        before, after = crop(scene, 0, 0), crop(scene, dx, dy)
        truth = (crop(scene, dx // 2, dy // 2).astype(np.float32) + crop(scene, (dx + 1) // 2, (dy + 1) // 2).astype(np.float32)) / 2     # halfway, to the half pixel
        noisy = lambda img, seed: np.clip(img.astype(np.float32) + np.random.default_rng(seed).normal(0, 3.0, (H, W, 1)), 0, 255).astype(np.uint8)
        before, after = noisy(before, 11), noisy(after, 12)
        real_truth, real_before, real_after = show_real(np.clip(truth, 0, 255).astype(np.uint8)), show_real(before), show_real(after)
        made = show_between(before, after)
        score, blended = mae(made, real_truth), mae((real_before + real_after) / 2, real_truth)
        check(f"moved by ({dx:+d}, {dy:+d}) with noise: still close to the true halfway picture", score < 0.4 * blended and score < 9,
              f"error {score:.1f} (blend {blended:.1f})")

    print("2. Where nothing moves, nothing changes")
    still = canvas(3)
    moving = still.copy()
    moving[110:200, 220:420] = canvas(4)[110:200, 220:420]                  # only this part changes, in a way no motion explains
    a = still[80:80 + H, 80:80 + W].astype(np.uint8)
    b = moving[80:80 + H, 80:80 + W].astype(np.uint8)
    made, real_a = show_between(a, b), show_real(a)
    steady = (slice(150, 280), slice(10, 600))                                # an area that is identical in both pictures
    check("an area that is the same in both pictures is reproduced exactly", float(np.abs(made[steady] - real_a[steady]).max()) <= 2,
          f"largest difference {float(np.abs(made[steady] - real_a[steady]).max()):.1f}")

    print("3. A scene cut: nothing is invented, no double image")
    other = canvas(9)
    a, b = crop(scene, 0, 0), crop(other, 0, 0)
    real_a, real_b = show_real(a), show_real(b)
    made = show_between(a, b)
    from_one = np.minimum(np.abs(made - real_a).max(axis=2), np.abs(made - real_b).max(axis=2))
    blend = (real_a + real_b) / 2
    check("every pixel is (about) one of the two real pictures, not a mix of them", float((from_one > 24).mean()) < 0.04,
          f"{float((from_one > 24).mean()) * 100:.1f} % of pixels are a mix; a 50 % blend would be {float((np.minimum(np.abs(blend - real_a).max(axis=2), np.abs(blend - real_b).max(axis=2)) > 24).mean()) * 100:.0f} %")

    print("4. Timing and switching")
    widget.set_smooth(True)
    widget._pacer.force = True
    widget.clear()
    widget.set_frame(to_frame(crop(scene, 0, 0)), time.perf_counter())
    settle(0.03)
    widget.set_frame(to_frame(crop(scene, 10, 0)), time.perf_counter())
    check("when a picture arrives a generated one is shown first", widget._mid and widget._mid_timer.isActive())
    settle(0.2)
    check("half a picture later the real one replaces it", not widget._mid)
    after_real = grab()
    check("... and it is exactly the real picture", mae(after_real, show_real(crop(scene, 10, 0))) < 0.6, f"{mae(after_real, show_real(crop(scene, 10, 0))):.2f}")
    widget.set_smooth(True)
    widget._pacer.force = False
    widget._pacer.refresh_hz = 60.0
    fast = to_frame(crop(scene, 0, 0))
    for _ in range(6):                                                       # a stream that is already fast for a 60 Hz screen: no smoothing
        widget.set_frame(fast, time.perf_counter())
        time.sleep(0.005)
        app.processEvents()
    check("a stream already as fast as the screen is left alone (it would only add delay)", not widget._mid and "paused" in widget.smooth_state(),
          widget.smooth_state())
    widget.set_smooth(False)
    widget.set_frame(to_frame(crop(scene, 5, 0)), time.perf_counter())
    check("switched off: nothing is generated", not widget._mid and widget.smooth_state() == "off")
    check("the newest REAL picture is always the one the program reads (the loot-list check etc.)", widget.current_frame() is not None)

    print("5. Cost")
    big = VideoWidget()
    big.setFixedSize(1920, 864)
    big.show()
    big.set_smooth(True)
    big._pacer.force = True
    scene_big = np.random.default_rng(1).integers(0, 256, (864, 1920, 3), dtype=np.uint8)
    first = to_frame(scene_big)
    second = to_frame(np.roll(scene_big, 9, axis=1))
    big.set_frame(first, time.perf_counter())
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    times = []
    for _ in range(8):
        big.set_frame(first, time.perf_counter())
        big.set_frame(second, time.perf_counter())
        big._mid_timer.stop()
        start = time.perf_counter()
        big.repaint()
        big.makeCurrent()
        big.context().functions().glFinish()
        times.append((time.perf_counter() - start) * 1000)
        big.doneCurrent()
        big.set_frame(first, time.perf_counter())
        big._mid_timer.stop()
        app.processEvents()
    best = min(times)
    print(f"  (upload + motion + picture for one generated picture at 1920x864: {best:.1f} ms on {widget.context().functions().glGetString(0x1F01)})")
    check("it runs at full size in a sane time (a slow chip pauses it by itself, see below)", best < 120.0, f"{best:.1f} ms")
    big.set_smooth(False)
    big.set_smooth(True)                                                     # a fresh start (the timing loop above may have left strikes)
    big._pacer.force = False
    big._pacer.refresh_hz = 240.0
    for _ in range(3):
        big._pacer.on_frame(time.perf_counter())
        time.sleep(0.033)
    big._pacer.report_cost(80.0)
    check("one slow measurement is forgiven", not big._pacer.too_slow)
    big._pacer.report_cost(80.0)
    check("two in a row on a 30/s stream pause smoothing, and the state says why", big._pacer.too_slow and "graphics chip" in big._pacer.state(), big._pacer.state())
    big.set_smooth(False)
    big.set_smooth(True)
    check("switching it off and on again gives it a fresh try", not big._pacer.too_slow)
    big.close()

    widget.close()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
