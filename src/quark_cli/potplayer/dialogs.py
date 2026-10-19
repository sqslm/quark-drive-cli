"""Themed modal prompts with the small interface used by the plugin pages."""
from tkinter import simpledialog, ttk
from .window_ui import configure_window


class _Prompt(simpledialog.Dialog):
    def __init__(self, parent, title, message, *, entry=False, initial='', cancel=True):
        self.message, self.entry, self.initial, self.cancel_button = message, entry, initial, cancel
        self.result = None
        super().__init__(parent, title)
        # Entry.master points back to this dialog. Break that retained cycle
        # here, on the UI thread, before a later network worker can collect it.
        self.value = None
        self.parent = None

    def body(self, master):
        configure_window(self)
        ttk.Label(master, text=self.message, wraplength=460, justify='left').pack(padx=16, pady=14)
        if self.entry:
            self.value = ttk.Entry(master, width=46)
            self.value.insert(0, self.initial)
            self.value.pack(fill='x', padx=16, pady=(0, 14))
            self.value.selection_range(0, 'end')
            return self.value

    def buttonbox(self):
        frame = ttk.Frame(self, padding=(16, 0, 16, 14))
        if self.cancel_button:
            ttk.Button(frame, text='取消', command=self.cancel).pack(side='right')
        ttk.Button(frame, text='确定', command=self.ok).pack(side='right', padx=6)
        frame.pack(fill='x')
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def apply(self):
        self.result = self.value.get() if self.entry else True


def askstring(title, prompt, *, parent, initialvalue=''):
    return _Prompt(parent, title, prompt, entry=True, initial=initialvalue).result


def askyesno(title, message, *, parent=None):
    return _Prompt(parent, title, message).result is True


def showinfo(title, message, *, parent=None):
    _Prompt(parent, title, message, cancel=False)


showerror = showinfo
