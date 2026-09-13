import json
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(title="选择 Obsidian 笔记库", mustexist=True, parent=root)
root.destroy()
print(json.dumps({"path": path or None}, ensure_ascii=False))
