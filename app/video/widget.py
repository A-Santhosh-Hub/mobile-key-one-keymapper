"""OpenGL video widget: uploads Y/U/V planes and converts to RGB in a fragment shader.

This keeps the CPU free of per-pixel work; the GPU does colour conversion and scaling.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QVector2D, QVector3D
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLPixelTransferOptions, QOpenGLShader, QOpenGLShaderProgram,
                              QOpenGLTexture)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel

from .frame import YuvFrame
from .look import PictureLook
from .smooth import Pacer

GL_COLOR_BUFFER_BIT = 0x4000
GL_TRIANGLE_STRIP = 0x0005
GL_FLOAT = 0x1406
GL_FRAMEBUFFER = 0x8D40
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_NEAREST = 0x2801, 0x2800, 0x2600

BLOCK = 16                 # the motion search works on blocks of this many pixels
SEARCH_START = 64.0        # pixels: the first step of the search; it finds motion up to about twice this per picture (the halving goes down to half a pixel)

_VERTEX = """
attribute vec2 pos;
varying vec2 uv;
void main() {
    uv = vec2(pos.x * 0.5 + 0.5, 0.5 - pos.y * 0.5);
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

_FRAGMENT = """
varying vec2 uv;
uniform sampler2D ty;
uniform sampler2D tu;
uniform sampler2D tv;
uniform vec3 yuv_offset;
uniform vec3 yuv_scale;
uniform vec3 coef_r;
uniform vec3 coef_g;
uniform vec3 coef_b;
uniform vec2 step_uv;      // one output pixel (one source pixel when enlarging), in texture coordinates
uniform float sharpness;   // 0 = off
uniform vec3 adjust;       // brightness, contrast, saturation
void main() {
    float y = texture2D(ty, uv).r;
    if (sharpness > 0.0) {                                   // unsharp mask on the brightness plane only
        float blur = 0.25 * (texture2D(ty, uv + vec2(step_uv.x, 0.0)).r + texture2D(ty, uv - vec2(step_uv.x, 0.0)).r
                           + texture2D(ty, uv + vec2(0.0, step_uv.y)).r + texture2D(ty, uv - vec2(0.0, step_uv.y)).r);
        y = y + sharpness * 1.5 * (y - blur);
    }
    vec3 yuv = vec3(y, texture2D(tu, uv).r, texture2D(tv, uv).r);
    yuv = (yuv - yuv_offset) * yuv_scale;
    vec3 rgb = vec3(dot(coef_r, yuv), dot(coef_g, yuv), dot(coef_b, yuv));
    float luma = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
    rgb = mix(vec3(luma), rgb, adjust.z);                    // saturation
    rgb = (rgb - 0.5) * adjust.y + 0.5 + adjust.x;           // contrast, brightness
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""

# ---------------------------------------------------------------------------------------------------------------------------
# Smooth picture: the picture HALFWAY between two real pictures, made with motion compensation (no plain blending, so no ghosts).
#   pass 1 (motion): for each 8x8 block, find where it moved to between the previous and the next picture (a step-halving search on
#                    the brightness plane, coarse to fine, 73 candidates; the result goes to a small texture: dx, dy in quarter-pixels);
#   pass 2 (picture): for every pixel take the best of its four neighbouring blocks' motions (checked on that pixel's own surroundings), and
#                    average the previous picture moved forward and the next picture moved back. Where no motion explains the change (a scene
#                    cut, an explosion, a fast turn) it shows the nearest real picture instead: never a double image.
_VERTEX_130 = "#version 130\n" + _VERTEX

_MOTION = """#version 130
varying vec2 uv;
uniform sampler2D py;      // previous picture, brightness
uniform sampler2D ny;      // next picture, brightness
uniform vec2 texel;        // 1 / picture size
uniform float t;           // 0.5 = halfway
uniform float start_step;
const float LAMBDA = 0.0003;               // a little preference for small motion (flat areas stay still)
// How well the picture around this block matches when moved by d. Coarse steps compare a blurred, smaller copy of the pictures (a mip level),
// so that the search is guided by large shapes first and by fine detail last.
float cost(vec2 d, float level, float spacing) {
    float e = 0.0;
    for (int j = 0; j < 4; j++) {
        for (int i = 0; i < 4; i++) {
            vec2 s = (vec2(float(i), float(j)) - 1.5) * spacing;
            float a = textureLod(py, uv + (s - d * t) * texel, level).r;
            float b = textureLod(ny, uv + (s + d * (1.0 - t)) * texel, level).r;
            e += abs(a - b);
        }
    }
    return e / 16.0;
}
void main() {
    vec2 best = vec2(0.0);
    float step_px = start_step;
    for (int k = 0; k < 8; k++) {
        float level = max(0.0, log2(step_px) - 1.0);
        float spacing = 4.0 * max(1.0, step_px / 4.0);
        float lam = (level < 0.5) ? LAMBDA : 0.0;               // (blurred comparisons differ too little for a penalty to make sense)
        float best_cost = cost(best, level, spacing) + lam * length(best);
        vec2 centre = best;
        for (int j = -1; j <= 1; j++) {
            for (int i = -1; i <= 1; i++) {
                if (i == 0 && j == 0) continue;
                vec2 d = centre + vec2(float(i), float(j)) * step_px;
                float c = cost(d, level, spacing) + lam * length(d);
                if (c < best_cost) { best_cost = c; best = d; }
            }
        }
        step_px *= 0.5;
    }
    vec2 v = clamp(floor(best * 4.0 + 2048.5), 0.0, 4095.0);            // quarter pixels, 12 bits each, split over two 8-bit channels
    vec2 hi = floor(v / 256.0);
    gl_FragColor = vec4(hi.x, v.x - hi.x * 256.0, hi.y, v.y - hi.y * 256.0) / 255.0;
}
"""

_SYNTH = """#version 130
varying vec2 uv;
uniform sampler2D py;
uniform sampler2D pu;
uniform sampler2D pv;
uniform sampler2D ny;
uniform sampler2D nu;
uniform sampler2D nv;
uniform sampler2D mv;
uniform vec2 texel;
uniform vec2 frame_px;
uniform vec2 mv_dim;
uniform float block;
uniform float t;
uniform float lod;         // how much smaller than the picture the window is (mip level)
uniform vec3 yuv_offset;
uniform vec3 yuv_scale;
uniform vec3 coef_r;
uniform vec3 coef_g;
uniform vec3 coef_b;
uniform vec2 step_uv;
uniform float sharpness;
uniform vec3 adjust;
const float TRUST_FULL = 0.025;            // a pixel whose two moved copies differ by less than this (0..1) is fully motion-compensated
const float TRUST_NONE = 0.060;            // ... and by more than this it is not (the nearest real picture is shown)

vec2 motion(vec2 cell) {
    cell = clamp(cell, vec2(0.0), mv_dim - 1.0);
    vec4 b = floor(textureLod(mv, (cell + 0.5) / mv_dim, 0.0) * 255.0 + 0.5);
    return (vec2(b.r * 256.0 + b.g, b.b * 256.0 + b.a) - 2048.0) * 0.25;
}
float cost(vec2 p, vec2 d) {
    float e = 0.0;
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            vec2 s = vec2(float(i), float(j)) * 2.0 * texel;
            e += abs(textureLod(py, p + s - d * t * texel, lod).r - textureLod(ny, p + s + d * (1.0 - t) * texel, lod).r);
        }
    }
    vec2 a = p - d * t * texel, b = p + d * (1.0 - t) * texel;      // colour counts too: a match of brightness alone can be a different thing
    float chroma = abs(textureLod(pu, a, lod).r - textureLod(nu, b, lod).r) + abs(textureLod(pv, a, lod).r - textureLod(nv, b, lod).r);
    return e / 9.0 + 0.5 * chroma;
}
vec3 yuv_at(vec2 p, vec2 d, float trust) {
    vec2 a = p - d * t * texel, b = p + d * (1.0 - t) * texel;
    vec3 moved = mix(vec3(textureLod(py, a, lod).r, textureLod(pu, a, lod).r, textureLod(pv, a, lod).r),
                     vec3(textureLod(ny, b, lod).r, textureLod(nu, b, lod).r, textureLod(nv, b, lod).r), t);
    vec3 nearest = (t < 0.5) ? vec3(textureLod(py, p, lod).r, textureLod(pu, p, lod).r, textureLod(pv, p, lod).r)
                          : vec3(textureLod(ny, p, lod).r, textureLod(nu, p, lod).r, textureLod(nv, p, lod).r);
    return mix(nearest, moved, trust);
}
void main() {
    vec2 g = uv * frame_px / block - 0.5;
    vec2 c0 = floor(g);
    vec2 dn = motion(c0 + step(vec2(0.5), g - c0));                  // the block this pixel is in
    vec2 best = dn;
    float best_cost = cost(uv, dn);
    if (dn != vec2(0.0)) {                                           // a still part of the picture (the interface) inside a moving block?
        float quick = abs(textureLod(py, uv, lod).r - textureLod(ny, uv, lod).r) + 0.5 * (abs(textureLod(pu, uv, lod).r - textureLod(nu, uv, lod).r)
                                                                                           + abs(textureLod(pv, uv, lod).r - textureLod(nv, uv, lod).r));
        if (quick < 0.02) {                                          // (one pixel looked the same in both pictures: now check its surroundings)
            float z = cost(uv, vec2(0.0));
            if (z <= best_cost + 0.004) { best_cost = z; best = vec2(0.0); }
        }
    }
    if (best_cost > TRUST_FULL) {                                    // not explained yet: try the motion of the neighbouring blocks (an edge of a moving thing)
        for (int k = 0; k < 4; k++) {
            vec2 d = motion(c0 + vec2(float(k - (k / 2) * 2), float(k / 2)));
            if (d == best || d == dn) continue;
            float c = cost(uv, d);
            if (c < best_cost - 0.004) { best_cost = c; best = d; }
        }
    }
    float trust = 1.0 - smoothstep(TRUST_FULL, TRUST_NONE, best_cost);
    vec3 yuv = yuv_at(uv, best, trust);
    if (sharpness > 0.0) {
        float blur = 0.25 * (yuv_at(uv + vec2(step_uv.x, 0.0), best, trust).x + yuv_at(uv - vec2(step_uv.x, 0.0), best, trust).x
                           + yuv_at(uv + vec2(0.0, step_uv.y), best, trust).x + yuv_at(uv - vec2(0.0, step_uv.y), best, trust).x);
        yuv.x = yuv.x + sharpness * 1.5 * (yuv.x - blur);
    }
    yuv = (yuv - yuv_offset) * yuv_scale;
    vec3 rgb = vec3(dot(coef_r, yuv), dot(coef_g, yuv), dot(coef_b, yuv));
    float luma = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
    rgb = mix(vec3(luma), rgb, adjust.z);
    rgb = (rgb - 0.5) * adjust.y + 0.5 + adjust.x;
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""

# (Kr-derived) coefficients: r = y + c*v ; g = y - a*u - d*v ; b = y + b*u
_BT709 = (1.5748, 0.187324, 0.468124, 1.8556)
_BT601 = (1.402, 0.344136, 0.714136, 1.772)


class VideoWidget(QOpenGLWidget):
    frameSizeChanged = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 320)
        self.last_paint_latency_ms = 0.0

        self._pending: YuvFrame | None = None
        self._stamp = 0.0
        self._frame: YuvFrame | None = None       # last frame uploaded (for color + size)
        self._size = (0, 0)
        self._look = PictureLook()

        self._program: QOpenGLShaderProgram | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._sets: list[list[QOpenGLTexture]] = [[], []]      # two sets of Y/U/V textures: the newest picture and the one before it
        self._cur = 0                                          # the set that holds the newest picture
        self._have_prev = False                                # the other set holds the real picture before it
        self._tex_size = (0, 0)

        # smooth picture (frame generation): see smooth.py and the shaders above
        self._pacer = Pacer()
        self._mid = False                                      # show the generated halfway picture instead of the newest real one
        self._mid_timer = QTimer(self)
        self._mid_timer.setSingleShot(True)
        self._mid_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._mid_timer.timeout.connect(self._show_real)
        self._motion_program: QOpenGLShaderProgram | None = None
        self._synth_program: QOpenGLShaderProgram | None = None
        self._motion_fbo: QOpenGLFramebufferObject | None = None
        self._motion_key: tuple | None = None                  # which pair of pictures the motion field belongs to
        self._pair_id = 0                                      # changes with every upload
        self._counted_pair = None
        self._cost_sample = 0                                  # every 15th generated picture is timed (a one-off wait for the GPU)
        self.smooth_error = ""                                 # why smoothing cannot run on this graphics setup ("" = it can)
        self.generated_frames = 0

        self._label = QLabel("No device connected", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setStyleSheet("color: #6b7385; background: transparent; font-size: 14px; padding: 24px;")

    # -- public API ------------------------------------------------------------------------
    def set_look(self, look: PictureLook) -> None:
        """Brightness / contrast / saturation / sharpness / colour conversion; applied on the next paint."""
        self._look = look
        self.update()

    def look(self) -> PictureLook:
        return self._look

    # -- smooth picture --------------------------------------------------------------------
    @property
    def _textures(self) -> list[QOpenGLTexture]:
        return self._sets[self._cur]

    def set_smooth(self, enabled: bool) -> None:
        """Turn smooth picture (generated in-between pictures) on or off."""
        self._pacer.set_enabled(enabled)
        if not enabled:
            self._mid = False
            self._mid_timer.stop()
        self.update()

    def smooth_enabled(self) -> bool:
        return self._pacer.enabled

    def smooth_state(self) -> str:
        if self._pacer.enabled and self.smooth_error:
            return f"on, but not available here ({self.smooth_error})"
        return self._pacer.state()

    def _show_real(self) -> None:
        self._mid = False
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._label.setText(text)

    def set_frame(self, frame: YuvFrame, stamp: float) -> None:
        self._pending = frame
        self._stamp = stamp
        screen = self.screen()
        if screen is not None and screen.refreshRate() > 1:
            self._pacer.refresh_hz = screen.refreshRate()
        delay = self._pacer.on_frame(time.perf_counter())
        if delay is not None and not self.smooth_error:
            self._mid = True                      # first the picture halfway between the previous real one and this one ...
            self._mid_timer.start(max(1, round(delay * 1000)))      # ... and, half a picture later, the real one
        else:
            self._mid = False
            self._mid_timer.stop()
        self._label.hide()
        self.update()

    def clear(self) -> None:
        self._pending = None
        self._frame = None
        self._have_prev = False
        self._tex_size = (0, 0)                   # the next picture starts afresh: nothing before it
        self._mid = False
        self._mid_timer.stop()
        self._pacer.reset()
        self._size = (0, 0)
        self.frameSizeChanged.emit(0, 0)
        self._label.show()
        self.update()

    def current_frame(self):
        """The last decoded picture (a YuvFrame) or None. Read-only: used to tell the game from its menus."""
        return self._pending if self._pending is not None else self._frame

    def frame_size(self) -> tuple[int, int]:
        return self._size

    def target_rect(self) -> QRectF:
        """Where the phone picture is drawn inside this widget (letter-boxed), in widget coordinates."""
        return self.target_rect_for(self._size, self.width(), self.height())

    @staticmethod
    def target_rect_for(size: tuple[int, int], width: float, height: float) -> QRectF:
        if size[0] <= 0 or size[1] <= 0:
            return QRectF()
        scale = min(width / size[0], height / size[1])
        w, h = size[0] * scale, size[1] * scale
        return QRectF((width - w) / 2, (height - h) / 2, w, h)

    def map_to_device(self, pos: QPointF, clamp: bool = False) -> tuple[float, float] | None:
        """Widget position -> normalized (0..1) position inside the phone screen.

        Returns None outside the picture, or the nearest edge point when `clamp` is True."""
        target = self.target_rect()
        if target.isEmpty():
            return None
        if not clamp and not target.contains(pos):
            return None
        nx = (pos.x() - target.x()) / target.width()
        ny = (pos.y() - target.y()) / target.height()
        return (min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny)))

    # -- Qt events -------------------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._label.setGeometry(self.rect())

    def initializeGL(self) -> None:
        program = QOpenGLShaderProgram(self)
        program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX)
        program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT)
        if not program.link():
            raise RuntimeError("Video shader failed to compile: " + program.log())
        self._program = program

        vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        vbo.create()
        vbo.bind()
        quad = (-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
        import array
        data = array.array("f", quad).tobytes()
        vbo.allocate(data, len(data))
        vbo.release()
        self._vbo = vbo
        self._build_smooth_programs()

    def _build_smooth_programs(self) -> None:
        """The two shaders of smooth picture. If this graphics setup cannot compile them, smoothing stays off (the picture itself is unaffected)."""
        programs = []
        for name, source in (("motion", _MOTION), ("picture", _SYNTH)):
            program = QOpenGLShaderProgram(self)
            ok = (program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_130)
                  and program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, source) and program.link())
            if not ok:
                self.smooth_error = f"the {name} shader did not compile: {program.log().strip()[:160]}"
                return
            programs.append(program)
        self._motion_program, self._synth_program = programs

    def _make_textures(self, width: int, height: int) -> None:
        for texture_set in self._sets:
            for tex in texture_set:
                tex.destroy()
            texture_set.clear()
            for w, h in ((width, height), ((width + 1) // 2, (height + 1) // 2), ((width + 1) // 2, (height + 1) // 2)):
                tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
                tex.setFormat(QOpenGLTexture.TextureFormat.R8_UNorm)
                tex.setSize(w, h)
                tex.setMipLevels(tex.maximumMipLevels())          # mipmaps: a window smaller than the phone picture is
                tex.allocateStorage(QOpenGLTexture.PixelFormat.Red, QOpenGLTexture.PixelType.UInt8)
                tex.setMinMagFilters(QOpenGLTexture.Filter.LinearMipMapLinear, QOpenGLTexture.Filter.Linear)    # filtered, not aliased
                tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                texture_set.append(tex)
        self._cur = 0
        self._have_prev = False
        self._tex_size = (width, height)

    def _upload(self, frame: YuvFrame) -> None:
        if self._tex_size != (frame.width, frame.height) or not self._textures:
            self._make_textures(frame.width, frame.height)
            target = self._cur                                    # the first picture of a size: there is no picture before it
        else:
            target = 1 - self._cur                                # the newest picture stays where it is: it becomes "the one before"
            self._have_prev = True
        options = QOpenGLPixelTransferOptions()
        options.setAlignment(1)
        cw, ch = frame.chroma_size
        textures = self._sets[target]
        for tex, data, w, h in ((textures[0], frame.y, frame.width, frame.height),
                                (textures[1], frame.u, cw, ch),
                                (textures[2], frame.v, cw, ch)):
            tex.setData(0, 0, 0, w, h, 1, QOpenGLTexture.PixelFormat.Red, QOpenGLTexture.PixelType.UInt8,
                        data, options)
            tex.generateMipMaps()
        self._cur = target
        self._pair_id += 1
        self._frame = frame
        if self._size != (frame.width, frame.height):
            self._size = (frame.width, frame.height)
            self.frameSizeChanged.emit(frame.width, frame.height)

    def _draw_quad(self, program: QOpenGLShaderProgram) -> None:
        gl = self.context().functions()
        self._vbo.bind()
        loc = program.attributeLocation("pos")
        program.enableAttributeArray(loc)
        program.setAttributeBuffer(loc, GL_FLOAT, 0, 2, 0)
        gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        program.disableAttributeArray(loc)
        self._vbo.release()

    def _bind_pair(self, program: QOpenGLShaderProgram, names: tuple[str, ...], previous: bool, newest: bool, planes: int) -> None:
        """Bind the previous picture's planes then the newest picture's planes to texture units 0.. and tell the shader."""
        gl = self.context().functions()
        unit = 0
        for use, index in ((previous, 1 - self._cur), (newest, self._cur)):
            if not use:
                continue
            for plane in range(planes):
                self._sets[index][plane].bind(unit)
                gl.glUniform1i(program.uniformLocation(names[unit]), unit)
                unit += 1

    def _motion_pass(self, frame: YuvFrame) -> bool:
        """Pass 1: the motion between the previous and the newest real picture, one vector per block, into a small texture."""
        key = (self._pair_id, frame.width, frame.height)
        if key == self._motion_key and self._motion_fbo is not None:
            return True
        gl = self.context().functions()
        cells = (math.ceil(frame.width / BLOCK), math.ceil(frame.height / BLOCK))
        if self._motion_fbo is None or (self._motion_fbo.width(), self._motion_fbo.height()) != cells:
            self._motion_fbo = QOpenGLFramebufferObject(cells[0], cells[1])
            if not self._motion_fbo.isValid():
                self._motion_fbo = None
                self.smooth_error = "this graphics setup cannot render the motion texture"
                return False
            gl.glBindTexture(GL_TEXTURE_2D, self._motion_fbo.texture())
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)       # a motion vector is never averaged with its neighbour's
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        program = self._motion_program
        self._motion_fbo.bind()
        gl.glViewport(0, 0, cells[0], cells[1])
        program.bind()
        self._bind_pair(program, ("py", "ny"), True, True, 1)
        program.setUniformValue("texel", QVector2D(1.0 / frame.width, 1.0 / frame.height))
        gl.glUniform1f(program.uniformLocation("t"), 0.5)
        gl.glUniform1f(program.uniformLocation("start_step"), max(8.0, min(SEARCH_START, frame.height / 4.0)))
        self._draw_quad(program)
        program.release()
        self._motion_fbo.release()
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
        self._motion_key = key
        return True

    def paintGL(self) -> None:
        gl = self.context().functions()
        started = time.perf_counter()

        pending, self._pending = self._pending, None
        if pending is not None:
            self._upload(pending)
        frame = self._frame
        if frame is not None and self._mid and self._have_prev and self._synth_program is not None and not self.smooth_error:
            generated = self._motion_pass(frame)
        else:
            generated = False
        gl.glClearColor(0.043, 0.051, 0.071, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)
        if frame is None or self._program is None:
            return

        ratio = self.devicePixelRatioF()
        target = self.target_rect_for((frame.width, frame.height), self.width(), self.height())
        gl.glViewport(round(target.x() * ratio), round((self.height() - target.bottom()) * ratio),
                      round(target.width() * ratio), round(target.height() * ratio))

        program = self._synth_program if generated else self._program
        program.bind()
        if generated:
            self._bind_pair(program, ("py", "pu", "pv", "ny", "nu", "nv"), True, True, 3)
            gl.glActiveTexture(GL_TEXTURE0 + 6)
            gl.glBindTexture(GL_TEXTURE_2D, self._motion_fbo.texture())
            gl.glUniform1i(program.uniformLocation("mv"), 6)
            program.setUniformValue("texel", QVector2D(1.0 / frame.width, 1.0 / frame.height))
            program.setUniformValue("frame_px", QVector2D(frame.width, frame.height))
            program.setUniformValue("mv_dim", QVector2D(self._motion_fbo.width(), self._motion_fbo.height()))
            gl.glUniform1f(program.uniformLocation("block"), float(BLOCK))
            gl.glUniform1f(program.uniformLocation("t"), 0.5)
            gl.glUniform1f(program.uniformLocation("lod"), max(0.0, math.log2(max(1.0, frame.width / max(1.0, target.width() * ratio)))))
        else:
            for unit, (tex, name) in enumerate(zip(self._textures, ("ty", "tu", "tv"))):
                tex.bind(unit)
                gl.glUniform1i(program.uniformLocation(name), unit)     # must be a GLint, not a float
        mode = self._look.color_mode
        bt601, full_range = (frame.bt601, frame.full_range) if mode == "auto" else (mode.startswith("bt601"), mode.endswith("_full"))
        coef_c, coef_a, coef_d, coef_b = _BT601 if bt601 else _BT709
        if full_range:
            offset, yuv_scale = QVector3D(0, 128 / 255, 128 / 255), QVector3D(1, 1, 1)
        else:
            offset = QVector3D(16 / 255, 128 / 255, 128 / 255)
            yuv_scale = QVector3D(255 / 219, 255 / 224, 255 / 224)
        program.setUniformValue("yuv_offset", offset)
        program.setUniformValue("yuv_scale", yuv_scale)
        program.setUniformValue("coef_r", QVector3D(1, 0, coef_c))
        program.setUniformValue("coef_g", QVector3D(1, -coef_a, -coef_d))
        program.setUniformValue("coef_b", QVector3D(1, coef_b, 0))
        look = self._look
        view_w, view_h = max(1.0, target.width() * ratio), max(1.0, target.height() * ratio)
        program.setUniformValue("step_uv", QVector2D(max(1 / view_w, 1 / frame.width), max(1 / view_h, 1 / frame.height)))
        gl.glUniform1f(program.uniformLocation("sharpness"), float(look.sharpness))      # a real GLfloat (the setUniformValue overloads pick int)
        program.setUniformValue("adjust", QVector3D(look.brightness, look.contrast, look.saturation))

        self._draw_quad(program)
        program.release()
        if generated and self._motion_key is not None and self._motion_key[0] != self._counted_pair:
            self._counted_pair = self._motion_key[0]
            self.generated_frames += 1
            self._cost_sample += 1
            if self._cost_sample % 15 == 1:                    # the first one, then every 15th: wait for the GPU and see how long it took
                gl.glFinish()
                self._pacer.report_cost((time.perf_counter() - started) * 1000)

        if self._stamp:
            self.last_paint_latency_ms = (time.perf_counter() - self._stamp) * 1000
