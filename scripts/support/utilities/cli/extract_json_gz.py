import gzip
import json
import tkinter as tk
from tkinter import filedialog, scrolledtext


@LoggerManager().log_function_entry
@timeit("extract_json_gz")
def extract_json_gz(filepath):
    try:
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            data = json.load(f)
        return json.dumps(data, indent=4)
    except Exception as e:
        return f"❌ Error reading file: {e}"


@LoggerManager().log_function_entry
@timeit("select_file")
def select_file():
    filepath = filedialog.askopenfilename(
        title="Select a .json.gz file",
        filetypes=[("Gzipped JSON", "*.json.gz")]
    )
    if filepath:
        result = extract_json_gz(filepath)
        text_display.delete("1.0", tk.END)
        text_display.insert(tk.END, result)


# Set up GUI window
window = tk.Tk()
window.title("JSON.GZ Viewer")
window.geometry("800x600")

# Button to open file
select_button = tk.Button(window, text="📂 Select .json.gz File", command=select_file, font=("Segoe UI", 12))
select_button.pack(pady=10)

# Scrolled text box to display JSON
text_display = scrolledtext.ScrolledText(window, wrap=tk.WORD, font=("Consolas", 10))
text_display.pack(expand=True, fill="both", padx=10, pady=10)

# Run the app
window.mainloop()
