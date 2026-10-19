"""Shared PotPlayer-style charcoal controls. No page or account state."""
from __future__ import annotations

BACKGROUND = '#292929'
SURFACE = '#202020'
CONTROL = '#383838'
BORDER = '#4a4a4a'
TEXT = '#e6e6e6'
MUTED = '#aaaaaa'
ACCENT = '#ead658'
SELECTION = '#4a4630'


def apply_theme(window):
    from tkinter import ttk, font
    style = ttk.Style(window)
    if 'quark-potplayer' not in style.theme_names():
        style.theme_create('quark-potplayer', parent='clam')
        style.theme_use('quark-potplayer')
        families = set(font.families(window))
        family = next((name for name in ('Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI')
                       if name in families), 'TkDefaultFont')
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
            font.nametofont(name, root=window).configure(family=family, size=10)
        style.configure('.', background=BACKGROUND, foreground=TEXT, bordercolor=BORDER,
                        darkcolor=BACKGROUND, lightcolor=BACKGROUND,
                        troughcolor=SURFACE, focuscolor=ACCENT, font=(family, 10))
        style.configure('TButton', background=CONTROL, padding=(12, 5), relief='flat')
        style.map('TButton', background=[('disabled', BACKGROUND), ('pressed', SURFACE), ('active', '#474747')],
                  foreground=[('disabled', '#777777')], bordercolor=[('focus', ACCENT)])
        for widget in ('TEntry', 'TSpinbox', 'TCombobox'):
            style.configure(widget, fieldbackground=SURFACE, insertcolor=TEXT, arrowcolor=TEXT, padding=4)
            style.map(widget, fieldbackground=[('readonly', SURFACE), ('disabled', BACKGROUND)],
                      foreground=[('disabled', '#777777')], bordercolor=[('focus', ACCENT)])
        style.configure('TNotebook', tabmargins=(8, 6, 8, 0), borderwidth=0)
        style.configure('TNotebook.Tab', padding=(16, 8), background=SURFACE)
        style.map('TNotebook.Tab', background=[('selected', BACKGROUND), ('active', CONTROL)],
                  foreground=[('selected', ACCENT)])
        style.configure('TLabelframe', bordercolor=BORDER, relief='solid', borderwidth=1)
        style.configure('TLabelframe.Label', foreground=MUTED)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Treeview', background=SURFACE, fieldbackground=SURFACE,
                        foreground=TEXT, rowheight=28, borderwidth=0)
        style.map('Treeview', background=[('selected', SELECTION)], foreground=[('selected', ACCENT)])
        style.configure('Treeview.Heading', background=CONTROL, foreground=MUTED,
                        padding=(8, 6), relief='flat')
        style.map('Treeview.Heading', background=[('active', '#474747')])
        for widget in ('TCheckbutton', 'TRadiobutton'):
            style.configure(widget, indicatorbackground=SURFACE, indicatorforeground=SURFACE, padding=3)
            style.map(widget, background=[('active', BACKGROUND)], foreground=[('disabled', '#777777')],
                      indicatorbackground=[('selected', ACCENT), ('active', CONTROL)])
        # Clam's stock selected indicator is an X on Windows. Supply a real
        # check mark, retaining images on this interpreter's owning root.
        import tkinter as tk
        images = []
        for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
            image = tk.PhotoImage(master=window, width=16, height=16)
            image.put(BACKGROUND, to=(0, 0, 16, 16))
            image.put(BORDER if disabled else MUTED, to=(1, 1, 15, 15))
            image.put(ACCENT if selected and not disabled else SURFACE, to=(2, 2, 14, 14))
            if selected:
                for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                    image.put(MUTED if disabled else SURFACE, to=(x, y, x+2, y+2))
            images.append(image)
        window._root()._quark_check_images = images
        style.element_create('Quark.Check.indicator', 'image', images[0],
                             ('disabled', 'selected', images[3]), ('selected', images[1]),
                             ('disabled', images[2]), width=20, sticky='w')
        style.layout('TCheckbutton', [('Checkbutton.padding', {'sticky':'nswe', 'children':[
            ('Quark.Check.indicator', {'side':'left', 'sticky':''}),
            ('Checkbutton.focus', {'side':'left', 'sticky':'w', 'children':[
                ('Checkbutton.label', {'sticky':'nswe'})]})]})])
        style.configure('TScrollbar', background=CONTROL, arrowcolor=MUTED, borderwidth=0,
                        arrowsize=12, troughcolor=SURFACE)
        style.map('TScrollbar', background=[('active', '#555555')])
        style.configure('TProgressbar', background=ACCENT, troughcolor=SURFACE, borderwidth=0)
        # Classic Tk widgets used by QR images, text logs and modal bodies.
        for pattern, value in (
            ('*background', BACKGROUND), ('*foreground', TEXT), ('*insertBackground', TEXT),
            ('*selectBackground', SELECTION), ('*selectForeground', ACCENT),
            ('*activeBackground', CONTROL), ('*activeForeground', TEXT),
            ('*highlightBackground', BORDER), ('*highlightColor', ACCENT),
            ('*Text.background', SURFACE), ('*Entry.background', SURFACE),
            ('*Button.background', CONTROL), ('*Button.relief', 'flat'),
        ):
            window.option_add(pattern, value, 60)
    else:
        style.theme_use('quark-potplayer')
    window.configure(background=BACKGROUND)
    # Existing classic widgets (e.g. QR widgets created before this call).
    def paint(widget):
        kind = widget.winfo_class()
        if kind == 'Scrollbar':
            widget.configure(background=CONTROL, activebackground=BORDER, troughcolor=SURFACE,
                             highlightthickness=0)
        if kind in ('Frame', 'Label', 'Button', 'Entry', 'Text', 'Toplevel', 'Tk'):
            widget.configure(background=SURFACE if kind in ('Entry', 'Text') else
                             CONTROL if kind == 'Button' else BACKGROUND)
            if kind in ('Label', 'Button', 'Entry', 'Text'):
                widget.configure(foreground=TEXT)
        for child in widget.winfo_children():
            paint(child)
    paint(window)
