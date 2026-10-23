"""Fluent visual preview panel. All data/actions are explicitly injected.

Standard-library Tk only. No tray_common/personal_devices import, no file IO,
no startup or device actions. Gradients are static PNG surfaces, not animations.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, font as tkfont
from collections import OrderedDict

from fluent_theme import PALETTE as P, SCENES, demo_readings, gradient_png, bgra_png, mix, hex_color, state_color


def rounded(canvas, box, radius, **options):
    x1, y1, x2, y2 = box
    r = min(radius, (x2-x1)/2, (y2-y1)/2)
    return canvas.create_polygon(x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
        x2, y2-r, x2, y2, x2-r, y2, x1+r, y2, x1, y2, x1, y2-r,
        x1, y1+r, x1, y1, smooth=True, splinesteps=24, **options)


def device_icon(canvas, key, x, y, size, color):
    def q(v): return v*size/24
    line = {"fill": color, "width": max(1.2, size/15), "capstyle": "round", "joinstyle": "round"}
    def path(*points):
        coords = []
        for xx, yy in zip(points[::2], points[1::2]): coords.extend((x+q(xx), y+q(yy)))
        canvas.create_line(*coords, **line)
    if key == "mouse":
        rounded(canvas, (x+q(5), y+q(2), x+q(19), y+q(23)), q(7), fill="", outline=color, width=line["width"])
        path(12, 3, 12, 10)
        path(6, 10, 18, 10)
    elif key == "keyboard":
        rounded(canvas, (x+q(1), y+q(5), x+q(23), y+q(20)), q(3), fill="", outline=color, width=line["width"])
        for yy in (9, 13):
            for xx in (5, 9, 13, 17): path(xx, yy, xx+1, yy)
        path(7, 17, 17, 17)
    else:
        path(3, 9, 6, 5, 18, 5, 21, 9, 23, 19, 20, 21, 16, 16, 8, 16, 4, 21, 1, 19, 3, 9)
        path(6, 9, 6, 14)
        path(3.5, 11.5, 8.5, 11.5)
        for xx, yy in ((16, 11), (19, 9)):
            canvas.create_oval(x+q(xx-.6), y+q(yy-.6), x+q(xx+.6), y+q(yy+.6), fill=color, outline="")


class FluentPanel:
    def __init__(self, root, *, material="acrylic", scene="normal", preview=True, rows=None,
                 title=None, on_scene=None, on_float=None, on_refresh=None, on_settings=None):
        self.root, self.on_scene, self.on_float = root, on_scene, on_float
        self.preview = preview
        self.on_refresh, self.on_settings = on_refresh, on_settings
        self.status_text = "演示数据，不读取设备、不修改配置。" if preview else "正在读取设备电量…"
        self.status_error = False
        self.backend_status = "后台状态待确认"
        self.scene = scene
        self.rows = tuple(rows) if rows is not None else demo_readings(scene) if preview else ()
        self.transition = None
        self._transition_failed = False
        self.closed = False
        self._resize_job = None
        self.images = []
        self.gradient_cache = OrderedDict()
        self.ring_cache = OrderedDict()
        self._last_size = None
        self.float_visible = False
        self._details = None
        self.backdrop = None
        self.scale = min(2.5, max(1.0, root.winfo_fpixels("1i")/96))
        # Keep a complete design visible even on a smaller work area.
        self.scale = min(self.scale, (root.winfo_screenwidth()-100)/1000,
                         (root.winfo_screenheight()-150)/640)
        self.scale = max(.8, self.scale)
        self.s = lambda value: round(value*self.scale)
        self.seam = self.s(84)
        self.family = "Microsoft YaHei UI"
        self.number_family = "Segoe UI"
        self._fonts = {}
        root.title(title or ("设备电量 · Fluent 视觉预览" if preview else "我的设备电量"))
        root.geometry(f"{self.s(1100)}x{self.s(670)}")
        root.minsize(self.s(940), self.s(630))
        root.configure(bg=P["bg"])
        self.canvas = tk.Canvas(root, bg=P["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._style()
        # Native title/taskbar icon without an external image/font dependency.
        from ring_render import Canvas
        icon = Canvas(32, 32)
        icon.plate(9, color=(24, 36, 52), alpha=255)
        icon.ring(16, 16, 12, 3, (104, 223, 189), "percent", .8)
        self.icon_image = tk.PhotoImage(master=root, data=bgra_png(icon.premultiplied_bgra(), 32, 32), format="png")
        root.iconphoto(True, self.icon_image)
        self.refresh_button = ttk.Button(root, text="刷新预览" if preview else "立即刷新", style="Preview.Accent.TButton", command=self.refresh)
        self.details_button = ttk.Button(root, text="设备详情", style="Preview.TButton", command=self.open_details)
        self.float_button = ttk.Button(root, text="试用浮窗" if preview else "显示电量环", style="Preview.TButton", command=self.toggle_float)
        self.settings_button = ttk.Button(root, text="设置与后台", style="Preview.TButton", command=lambda: self.on_settings() if self.on_settings else None)
        self.scene_buttons = {key: ttk.Button(root, text=label, style="Preview.TButton", command=lambda k=key: self.set_scene(k)) for key, label in SCENES} if preview else {}
        self.material_choice = tk.StringVar(value={"acrylic": "Acrylic · 磨砂", "mica": "Mica · 云母", "solid": "深色实底"}.get(material, "深色实底"))
        self.material_combo = ttk.Combobox(root, textvariable=self.material_choice, state="readonly",
            values=("Acrylic · 磨砂", "Mica · 云母", "深色实底"), style="Preview.TCombobox", takefocus=True)
        self.material_combo.bind("<<ComboboxSelected>>", self._material_changed)
        root.update_idletasks()
        try:
            from native_transition import TransitionStrip
            self.transition = TransitionStrip(root)
        except (ImportError, OSError):
            pass
        self.apply_material(material)
        self.canvas.bind("<Configure>", self._configured)
        root.bind("<Configure>", self._root_configured, add=True)
        root.bind("<Unmap>", self._root_unmapped, add=True)
        root.bind("<Map>", self._root_configured, add=True)
        root.bind("<Destroy>", self._destroyed, add=True)
        self.redraw()

    def font(self, size, bold=False, number=False):
        key = (size, bold, number)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(root=self.root, family=self.number_family if number else self.family,
                size=-self.s(size), weight="bold" if bold else "normal")
        return self._fonts[key]

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Preview.TButton", background="#253346", foreground=P["text"], borderwidth=0,
            focusthickness=1, focuscolor=P["blue"], padding=(self.s(13), self.s(7)), font=self.font(12))
        st.map("Preview.TButton", background=[("active", "#35465d"), ("pressed", "#1b293a")])
        st.configure("Preview.Selected.TButton", background="#354c65", foreground=P["text"], font=self.font(12, True))
        st.configure("Preview.Accent.TButton", background=P["mint"], foreground="#092a24", font=self.font(12, True))
        st.map("Preview.Accent.TButton", background=[("active", "#98efd3"), ("pressed", "#4bbd9c")])
        st.configure("Preview.TCombobox", fieldbackground="#223045", background="#223045", foreground=P["text"],
            arrowcolor=P["secondary"], borderwidth=0, padding=self.s(8), font=self.font(12))
        st.map("Preview.TCombobox", fieldbackground=[("readonly", "#223045")], foreground=[("readonly", P["text"])])
        self.root.option_add("*TCombobox*Listbox.background", "#223045")
        self.root.option_add("*TCombobox*Listbox.foreground", P["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#354c65")

    def apply_material(self, kind):
        from win_backdrop import apply_backdrop, style_chrome
        self.glass_margins = (self.s(16), self.s(16), self.seam, self.s(16))
        self._transition_failed = False
        self.backdrop = apply_backdrop(self.root, kind=kind, force_solid=kind == "solid",
                                       frame_margins=self.glass_margins)
        if self.backdrop.applied == "solid":
            style_chrome(self.root)
        self.client_bg = self.backdrop.client_background
        self.root.configure(bg=self.client_bg)
        self.canvas.configure(bg=self.client_bg)

    def _material_changed(self, _event=None):
        kind = {"Acrylic · 磨砂": "acrylic", "Mica · 云母": "mica", "深色实底": "solid"}[self.material_choice.get()]
        self.apply_material(kind)
        self.redraw()

    def _root_configured(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._update_transition()

    def _root_unmapped(self, event):
        if event.widget is self.root and self.transition is not None:
            self.transition.hide()

    def _update_transition(self):
        if self.closed or not hasattr(self, "client_bg"):
            return True
        if self.client_bg != "#000000":
            if self.transition is not None:
                self.transition.hide()
            return True
        if not self.root.winfo_viewable() or self.root.state() == "iconic":
            if self.transition is not None:
                self.transition.hide()
            return True
        if self.transition is None:
            return False
        return self.transition.present(self.s(30), self.seam, color_rgb=(24, 37, 58), side_margin_px=self.s(16))

    def _configured(self, _event=None):
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(100, self.redraw)

    def _destroyed(self, event):
        if event.widget is self.root:
            self.closed = True
            if self._resize_job:
                try: self.root.after_cancel(self._resize_job)
                except tk.TclError: pass
            self._resize_job = None
            if self.transition is not None:
                self.transition.destroy()
                self.transition = None
            self.images.clear()
            self.gradient_cache.clear()
            self.ring_cache.clear()

    def text(self, x, y, text, size=12, color=None, bold=False, anchor="nw", **kwargs):
        return self.canvas.create_text(round(x), round(y), text=text, font=self.font(size, bold),
            fill=color or P["text"], anchor=anchor, **kwargs)

    def image(self, x, y, data):
        image = tk.PhotoImage(master=self.root, data=data, format="png")
        self.images.append(image)
        self.canvas.create_image(round(x), round(y), image=image, anchor="nw")
        return image

    def gradient(self, box, first, last, radius=0):
        x, y, right, bottom = map(round, box)
        key = (max(1, right-x), max(1, bottom-y), first, last, radius)
        image = self.gradient_cache.get(key)
        if image is None:
            image = tk.PhotoImage(master=self.root, data=gradient_png(*key), format="png")
            self.gradient_cache[key] = image
            while len(self.gradient_cache) > 8:
                self.gradient_cache.popitem(last=False)
        self.gradient_cache.move_to_end(key)
        self.images.append(image)
        self.canvas.create_image(x, y, image=image, anchor="nw")

    def pill(self, x, y, label, color=None):
        color = color or P["blue"]
        width = self.font(11).measure(label)+self.s(30)
        rounded(self.canvas, (x, y, x+width, y+self.s(25)), self.s(12),
            fill=hex_color(mix(P["surface"], color, .17)), outline=hex_color(mix(P["surface"], color, .35)), width=1)
        self.canvas.create_oval(x+self.s(10), y+self.s(10), x+self.s(14), y+self.s(14), fill=color, outline="")
        self.text(x+self.s(20), y+self.s(5), label, 11, color)

    def redraw(self):
        self._resize_job = None
        if self.closed or not self.canvas.winfo_exists(): return
        c, s = self.canvas, self.s
        width, height = max(1, c.winfo_width()), max(1, c.winfo_height())
        if width < self.s(700) or height < self.s(520):
            return  # Wait for the first real Configure instead of placing off-screen.
        c.delete("all")
        self.images.clear()
        if self._last_size != (width, height):
            self.gradient_cache.clear()
            self.ring_cache.clear()
            self._last_size = (width, height)
        if self.client_bg != "#000000":
            self.gradient((0, 0, width, height), "#18253a", P["bg"])
        else:
            # Restrict native material to header/gutters. Ordinary client pixels
            # preserve opaque card/text colors instead of additive GDI-on-glass.
            left, right, top, bottom = self.glass_margins
            self.gradient((left, top, width-right, height-bottom), "#18253a", P["bg"], 0)
        pad = s(28)
        # The native glass ends at `seam`; a click-through gradient strip fades
        # from transparent to exactly the body colour, and every piece of ink
        # starts below it, so nothing depends on a busy translucent background.
        seam = self.seam
        self.text(pad, seam+s(8), "P E R S O N A L  /  B A T T E R Y", 10, P["secondary"], True)
        self.text(pad, seam+s(26), "设备电量", 29, bold=True)
        self.text(pad, seam+s(72), "三台设备，一个安静的角落。", 13, P["secondary"])
        self.text(width-pad, seam+s(72), "视觉预览 · 演示数据" if self.preview else "实时设备电量", 12, P["blue"], anchor="ne")
        x = pad
        row_y = seam+s(98)
        if self.preview:
            for key, label in SCENES:
                bw = s(112 if key == "charging" else 78)
                self.scene_buttons[key].configure(style="Preview.Selected.TButton" if key == self.scene else "Preview.TButton")
                self.scene_buttons[key].place(x=x, y=row_y, width=bw, height=s(34))
                x += bw+s(6)
        else:
            self.settings_button.place(x=pad, y=row_y, width=s(140), height=s(34))
            self.text(pad+s(155), row_y+s(9), self.backend_status, 11, P["secondary"], width=s(205))
            x = pad+s(370)
        # Keep actionable controls outside the glass region: Tk/GDI controls
        # on extended pixels can wash out their background and text colors.
        self.details_button.place(x=x+s(10), y=row_y, width=s(104), height=s(34))
        self.refresh_button.place(x=x+s(126), y=row_y, width=s(112), height=s(34))
        self.text(width-pad-s(246), row_y+s(9), "背景材质", 12, P["secondary"])
        self.material_combo.place(x=width-pad-s(168), y=row_y, width=s(168), height=s(34))
        top = seam+s(144)
        card_h = max(s(232), min(s(276), height-top-s(186)))
        gap = s(16)
        card_w = (width-2*pad-2*gap)/3
        for i, reading in enumerate(self.rows):
            self._card(pad+i*(card_w+gap), top, card_w, card_h, reading)
        rail_y = top+card_h+s(18)
        rail_h = s(112)
        self.gradient((pad, rail_y, width-pad, rail_y+rail_h), "#1a2a3d", "#172130", s(18))
        rounded(c, (pad, rail_y, width-pad, rail_y+rail_h), s(18), fill="", outline="#35465b", width=1)
        self.text(pad+s(24), rail_y+s(15), "三个环，清晰一点。" if self.preview else "任务栏电量环", 17, bold=True)
        self.text(pad+s(24), rail_y+s(43), "系统字体 · 细弧线 · 无常驻动画" if self.preview else "左下角任务栏内显示 · 全屏自动隐藏", 11, P["secondary"])
        self.float_button.place(x=pad+s(24), y=rail_y+s(70), width=s(155), height=s(31))
        import fluent_ring_render as rings
        ring_key = (s(70), tuple((r.level, r.coarse, bool(r.error)) for r in self.rows))
        cached = self.ring_cache.get(ring_key)
        if cached is None:
            data, rw, rh = rings.render_panel(self.rows, height_px=s(70), plate_alpha=0)
            image = tk.PhotoImage(master=self.root, data=bgra_png(data, rw, rh), format="png")
            cached = (image, rw, rh)
            self.ring_cache[ring_key] = cached
            while len(self.ring_cache) > 4:
                self.ring_cache.popitem(last=False)
        self.ring_cache.move_to_end(ring_key)
        image, rw, rh = cached
        self.images.append(image)
        c.create_image(width-pad-rw-s(25), rail_y+s(18), image=image, anchor="nw")
        self.text(width-pad-s(25), rail_y+s(95), "鼠标  /  键盘  /  手柄", 10, P["secondary"], anchor="ne")
        material = getattr(self.backdrop, "applied", "solid")
        accepted = getattr(self.backdrop, "native_accepted", False)
        label = (f"Windows {material.capitalize()}" if accepted and material != "solid"
                 else "深色实底" if self.material_choice.get() == "深色实底" else "深色实底 · 兼容回退")
        self.text(pad, height-s(36), self.status_text, 11, P["amber"] if self.status_error else P["muted"], width=max(s(300), width-s(280)))
        self.text(width-pad, height-s(36), label, 10, P["muted"], anchor="ne")
        if not self._update_transition() and self.client_bg == "#000000":
            # No partially configured overlay/paint hack: readable solid fallback.
            self.apply_material("solid")
            self._transition_failed = True
            self.root.after_idle(self.redraw)

    def _card(self, x, y, width, height, reading):
        s, c = self.s, self.canvas
        tone = {"mouse": P["mint"], "keyboard": P["blue"], "gamepad": P["violet"]}[reading.key]
        first = hex_color(mix("#1d2a3b", tone, .085))
        self.gradient((x, y, x+width, y+height), first, "#182130", s(20))
        rounded(c, (x, y, x+width, y+height), s(20), fill="", outline="#3b4c61", width=1)
        rounded(c, (x+s(24), y+s(23), x+s(64), y+s(63)), s(11),
            fill=hex_color(mix("#213044", tone, .16)), outline="")
        device_icon(c, reading.key, x+s(33), y+s(31), s(23), tone if reading.ok else P["muted"])
        self.text(x+s(78), y+s(23), reading.title, 16, bold=True)
        subtitle = reading.subtitle if self.preview else {"mouse": "星闪无线接收器", "keyboard": "Lofree Flow Lite · 无线接收器", "gamepad": "XInput 系统电池状态"}[reading.key]
        self.text(x+s(78), y+s(49), subtitle, 11, P["secondary"])
        color = state_color(reading)
        value = str(reading.level) if reading.ok and reading.level is not None else reading.coarse if reading.ok else "—"
        number = bool(reading.ok and reading.level is not None)
        size = 54 if number else 40 if reading.ok else 54
        font = self.font(size, True, number)
        c.create_text(x+s(24), y+s(81), text=value, font=font, fill=color if reading.ok and reading.level is not None and reading.level < 25 else P["text"] if reading.ok else P["muted"], anchor="nw")
        if number:
            self.text(x+s(31)+font.measure(value), y+s(117), "%", 19, P["secondary"])
        if reading.ok:
            status = reading.status or "读数可用"
        else:
            error = reading.error or ""
            status = ("读取中" if "正在" in error else "未连接" if any(word in error for word in ("未连接", "未找到", "休眠")) else "读取异常")
        self.pill(x+s(24), y+s(157), status, color)
        left, right, yy = x+s(24), x+width-s(24), y+height-s(53)
        note = "精确百分比" if number else "四档区间 · 非百分比"
        if not reading.ok:
            c.create_line(left, yy+s(3), right, yy+s(3), fill=P["track"], width=s(3), dash=(s(4), s(4)))
            note = "等待设备重新连接"
        elif reading.coarse == "有线":
            self.text(left, yy-s(3), "有线模式不显示电量", 11, P["secondary"])
            note = "系统连接状态"
        elif number:
            rounded(c, (left, yy, right, yy+s(6)), s(3), fill=P["track"], outline="")
            progress = max(0, min(100, reading.level))/100*(right-left)
            if progress > 0:
                rounded(c, (left, yy, left+progress, yy+s(6)), min(s(3), progress/2), fill=color, outline="")
        else:
            steps = {"极低档": 1, "低档": 2, "中档": 3, "高档": 4}.get(reading.coarse, 0)
            segment = (right-left-s(7)*3)/4
            for i in range(4):
                xx = left+i*(segment+s(7))
                rounded(c, (xx, yy, xx+segment, yy+s(6)), s(3), fill=color if i < steps else P["track"], outline="")
        self.text(left, y+height-s(31), note, 10, P["muted"])

    def set_readings(self, rows):
        if self.closed: return
        self.rows = tuple(rows)
        self.redraw()
        self._update_details()

    def set_status(self, text, error=False):
        if self.closed: return
        self.status_text, self.status_error = str(text), bool(error)
        self.redraw()

    def set_backend_status(self, text):
        if self.closed: return
        if self.backend_status != str(text):
            self.backend_status = str(text)
            self.redraw()

    def set_refreshing(self, busy):
        if self.closed: return
        self.refresh_button.configure(state="disabled" if busy else "normal")

    def set_scene(self, scene):
        if not self.preview:
            raise RuntimeError("live UI cannot select synthetic scenes")
        self.rows = demo_readings(scene)
        self.scene = scene
        self.redraw()
        if self.on_scene: self.on_scene(self.rows)

    def refresh(self):
        if self.preview:
            self.set_scene(self.scene)
        elif self.on_refresh:
            self.on_refresh()

    def toggle_float(self):
        if self.on_float:
            result = self.on_float()
            if self.preview:
                self.set_float_visible(bool(result))

    def set_float_visible(self, visible):
        self.float_visible = visible
        if self.preview:
            self.float_button.configure(text="隐藏试用浮窗" if visible else "试用浮窗")

    def _details_content(self):
        heading = "设备详情 / 视觉预览" if self.preview else "设备详情 / 实际读取结果"
        lines = [heading, ""]
        for row in self.rows:
            lines.extend((f"{row.title}  ·  {row.display_value}", row.subtitle,
                          row.error or row.status, row.detail or "", getattr(row, "extra", ""), ""))
        lines.append("演示数据，不读取硬件或修改系统设置。" if self.preview else "手柄只显示系统提供的档位；不会换算为虚构百分比。")
        lines.append("背景材质由系统决定表现；可切换为深色实底。")
        return "\n".join(lines)

    def _update_details(self):
        if self._details is not None and self._details.winfo_exists():
            text = self._details_text
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("end", self._details_content())
            text.configure(state="disabled")

    def open_details(self):
        if self._details is not None and self._details.winfo_exists():
            self._update_details()
            self._details.lift()
            return
        win = tk.Toplevel(self.root)
        self._details = win
        win.title("设备详情 · 演示数据" if self.preview else "设备读取详情")
        win.configure(bg=P["bg"])
        win.geometry(f"{self.s(630)}x{self.s(430)}")
        text = tk.Text(win, bg=P["bg"], fg=P["secondary"], relief="flat", bd=0, padx=self.s(24), pady=self.s(20),
            font=self.font(13), wrap="word", insertbackground=P["text"])
        text.pack(fill="both", expand=True)
        self._details_text = text
        self._update_details()
        from win_backdrop import style_chrome
        style_chrome(win)
        win.bind("<Escape>", lambda event: win.destroy())
