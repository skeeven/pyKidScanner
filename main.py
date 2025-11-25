#!/usr/bin/env python3
"""
Kid Scanner - Raspberry Pi / Ubuntu barcode scanner app for kids.

This app lets a child scan barcodes with a USB scanner. When a barcode
is recognized, it shows a big image, name, and description. When an
unknown barcode is scanned, it tries UPCitemdb to auto-fill details,
and if that fails, an adult can add it to the database.
"""

import os
import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Optional, Dict

from PIL import Image, ImageTk  # type: ignore
import requests  # NEW: for UPCitemdb API

DB_PATH = "items.db"
IMAGES_DIR = "images"
PLACEHOLDER_IMAGE = os.path.join(IMAGES_DIR, "placeholder.png")
NO_IMAGE_IMAGE = os.path.join(IMAGES_DIR, "no_image.png")
# NEW: UPCitemdb trial endpoint (no API key, limited quota)
UPCITEMDB_URL = (
    "https://api.upcitemdb.com/prod/trial/lookup"
)


class ItemDatabase:
    """SQLite database wrapper for barcode items."""

    def __init__(self, db_path: str) -> None:
        """
        Initialize the database wrapper.

        Ensures the database file and table exist.
        """
        self.db_path = db_path
        self._ensure_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Return a new SQLite connection."""
        return sqlite3.connect(self.db_path)

    def _ensure_db(self) -> None:
        """Create the items table if it does not exist."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    barcode TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    image_path TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_item(self, barcode: str) -> Optional[Dict[str, Optional[str]]]:
        """
        Fetch an item by barcode.

        Returns:
            dict with keys barcode, name, description, image_path
            or None if item does not exist.
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT barcode, name, description, image_path
                FROM items
                WHERE barcode = ?
                """,
                (barcode,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "barcode": row[0],
                "name": row[1],
                "description": row[2],
                "image_path": row[3],
            }
        finally:
            conn.close()

    def upsert_item(
        self,
        barcode: str,
        name: str,
        description: str,
        image_path: Optional[str],
    ) -> None:
        """
        Insert or update an item by barcode.

        Uses SQLite's ON CONFLICT to perform an UPSERT.
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO items (barcode, name, description, image_path)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(barcode) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    image_path = excluded.image_path
                """,
                (barcode, name, description, image_path),
            )
            conn.commit()
        finally:
            conn.close()


class KidScannerApp:
    """Tkinter GUI application for the Kid Scanner."""

    def __init__(self, root: tk.Tk, db: ItemDatabase) -> None:
        """
        Initialize the Kid Scanner application.

        Args:
            root: Root Tk instance.
            db: ItemDatabase instance for persistence.
        """
        self.root = root
        self.db = db
        self.fullscreen = False
        self.current_image_tk: Optional[ImageTk.PhotoImage] = None

        self.root.title("Kid Scanner")
        self.root.geometry("800x480")

        # Allow toggling fullscreen with F11, exit with Escape.
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)

        # NEW: scale image area based on screen size
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        # Use up to 80% of width, 40% of height for the image
        self.img_max_w = int(screen_w * 0.8)
        self.img_max_h = int(screen_h * 0.4)
        # Build the two main views.
        self._build_scan_view()
        self._build_add_item_view()

        # Show scan view first.
        self.show_scan_view()

        # Keep the barcode entry focused.
        self._ensure_barcode_focus()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_scan_view(self) -> None:
        """Build the main scan/kiosk view with left controls and right image."""
        self.scan_frame = tk.Frame(self.root)
        self.scan_frame.pack(fill=tk.BOTH, expand=True)

        # Create two main panels: LEFT (controls) and RIGHT (image)
        self.left_frame = tk.Frame(self.scan_frame)
        self.left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.right_frame = tk.Frame(self.scan_frame, width=self.img_max_w, height=self.img_max_h)
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.right_frame.pack_propagate(False)

        # ---------------------------
        # LEFT SIDE (labels + barcode)
        # ---------------------------

        # Title (smaller font)
        self.item_name_label = tk.Label(
            self.left_frame,
            text="Scan an item to begin",
            font=("Arial", 18, "bold"),
        )
        self.item_name_label.pack(anchor="w", pady=(0, 5))

        # Barcode label + entry
        upc_label = tk.Label(
            self.left_frame,
            text="UPC / Barcode:",
            font=("Arial", 12, "bold"),
        )
        upc_label.pack(anchor="w")

        self.barcode_entry = tk.Entry(
            self.left_frame,
            font=("Arial", 16),
            width=24
        )
        self.barcode_entry.pack(anchor="w", pady=(0, 10))
        self.barcode_entry.bind("<Return>", self._on_barcode_entered)

        # Last scanned label
        self.last_barcode_label = tk.Label(
            self.left_frame,
            text="",
            font=("Arial", 10)
        )
        self.last_barcode_label.pack(anchor="w", pady=5)

        # Description label
        desc_label = tk.Label(
            self.left_frame,
            text="Description:",
            font=("Arial", 12, "bold"),
        )
        desc_label.pack(anchor="w")

        # Description text box with scrollbar
        desc_frame = tk.Frame(self.left_frame)
        desc_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.item_desc_text = tk.Text(
            desc_frame,
            height=8,
            wrap=tk.WORD,
            font=("Arial", 12),
        )
        self.item_desc_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(desc_frame, command=self.item_desc_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.item_desc_text.configure(yscrollcommand=scrollbar.set)

        # Buttons section
        btn_frame = tk.Frame(self.left_frame)
        btn_frame.pack(pady=10)

        add_button = tk.Button(
            btn_frame,
            text="Add Item Manually",
            font=("Arial", 12, "bold"),
            command=self.show_add_item_view_with_current_barcode,
        )
        add_button.grid(row=0, column=0, padx=5)

        clear_button = tk.Button(
            btn_frame,
            text="Clear",
            font=("Arial", 12),
            command=self._clear_display,
        )
        clear_button.grid(row=0, column=1, padx=5)

        fullscreen_button = tk.Button(
            btn_frame,
            text="Toggle Fullscreen",
            font=("Arial", 12),
            command=self._toggle_fullscreen_btn,
        )
        fullscreen_button.grid(row=0, column=2, padx=5)

        # ---------------------------
        # RIGHT SIDE (image)
        # ---------------------------
        self.item_image_label = tk.Label(
            self.right_frame,
            bg="#dddddd",
            width=400,
            height=300,
        )
        self.item_image_label.pack(fill=tk.BOTH, expand=True)

        # Default image
        self._show_image(NO_IMAGE_IMAGE)

    def _build_add_item_view(self) -> None:
        """Build the add/edit item view."""
        self.add_frame = tk.Frame(self.root)

        title_label = tk.Label(
            self.add_frame,
            text="Add / Edit Item",
            font=("Arial", 22, "bold"),
        )
        title_label.grid(row=0, column=0, columnspan=2, pady=10)

        # Barcode (read-only).
        tk.Label(self.add_frame, text="Barcode:").grid(
            row=1, column=0, sticky=tk.E, padx=5, pady=5
        )
        self.add_barcode_var = tk.StringVar()
        self.add_barcode_entry = tk.Entry(
            self.add_frame,
            textvariable=self.add_barcode_var,
            state="readonly",
            width=30,
        )
        self.add_barcode_entry.grid(row=1, column=1, padx=5, pady=5)

        # Name.
        tk.Label(self.add_frame, text="Name:").grid(
            row=2, column=0, sticky=tk.E, padx=5, pady=5
        )
        self.add_name_var = tk.StringVar()
        self.add_name_entry = tk.Entry(
            self.add_frame,
            textvariable=self.add_name_var,
            width=30,
        )
        self.add_name_entry.grid(row=2, column=1, padx=5, pady=5)

        # Description.
        tk.Label(self.add_frame, text="Description:").grid(
            row=3, column=0, sticky=tk.NE, padx=5, pady=5
        )
        self.add_desc_text = tk.Text(self.add_frame, width=40, height=5)
        self.add_desc_text.grid(row=3, column=1, padx=5, pady=5)

        # Image path.
        tk.Label(self.add_frame, text="Image:").grid(
            row=4, column=0, sticky=tk.E, padx=5, pady=5
        )
        self.add_image_var = tk.StringVar()
        self.add_image_entry = tk.Entry(
            self.add_frame,
            textvariable=self.add_image_var,
            width=30,
        )
        self.add_image_entry.grid(
            row=4,
            column=1,
            padx=5,
            pady=5,
            sticky=tk.W,
        )

        image_button = tk.Button(
            self.add_frame,
            text="Browse...",
            command=self._choose_image_file,
        )
        image_button.grid(row=4, column=1, padx=5, pady=5, sticky=tk.E)

        # Save / Back buttons.
        save_button = tk.Button(
            self.add_frame,
            text="Save Item",
            font=("Arial", 12, "bold"),
            command=self._save_item,
        )
        save_button.grid(row=5, column=0, padx=5, pady=10)

        cancel_button = tk.Button(
            self.add_frame,
            text="Back to Scan",
            font=("Arial", 12),
            command=self.show_scan_view,
        )
        cancel_button.grid(row=5, column=1, padx=5, pady=10, sticky=tk.W)

    # ------------------------------------------------------------------
    # View switching
    # ------------------------------------------------------------------

    def show_scan_view(self) -> None:
        """Show the main scan view."""
        self.add_frame.pack_forget()
        self.scan_frame.pack(fill=tk.BOTH, expand=True)
        self.barcode_entry.focus_set()

    def show_add_item_view_with_current_barcode(self) -> None:
        """
        Show the add/edit view.

        Uses the last scanned barcode if available. If there is no
        barcode yet, show a message.
        """
        barcode = self.last_barcode_label.cget("text").replace(
            "Last barcode: ", ""
        ).strip()

        if not barcode:
            # Fall back to whatever is in the entry.
            barcode = self.barcode_entry.get().strip()

        if not barcode:
            messagebox.showinfo(
                "No barcode",
                "Scan an item first to pre-fill the barcode.",
            )
            return

        self._populate_add_form(barcode)
        self._show_add_view()

    def _show_add_view(self) -> None:
        """Switch to the add/edit view."""
        self.scan_frame.pack_forget()
        self.add_frame.pack(fill=tk.BOTH, expand=True)
        self.add_name_entry.focus_set()

    def _populate_add_form(self, barcode: str) -> None:
        """Fill the add/edit form with existing or new item data."""
        self.add_barcode_var.set(barcode)
        item = self.db.get_item(barcode)
        if item:
            self.add_name_var.set(item["name"] or "")
            self.add_desc_text.delete("1.0", tk.END)
            self.add_desc_text.insert("1.0", item["description"] or "")
            self.add_image_var.set(item["image_path"] or "")
        else:
            self.add_name_var.set("")
            self.add_desc_text.delete("1.0", tk.END)
            self.add_image_var.set("")

    # ------------------------------------------------------------------
    # Barcode handling
    # ------------------------------------------------------------------

    def _on_barcode_entered(self, event: tk.Event) -> None:
        """Handle barcode scanner input when Enter is pressed."""
        barcode = self.barcode_entry.get().strip()
        self.barcode_entry.delete(0, tk.END)

        if not barcode:
            return

        self.last_barcode_label.config(text=f"Last barcode: {barcode}")

        # 1) Check local database.
        item = self.db.get_item(barcode)
        if item:
            self._display_item(item)
            self.root.bell()
            return

        # 2) Try UPCitemdb (online lookup).
        api_item = self._lookup_upcitemdb(barcode)
        if api_item:
            # Ask if we should save this auto-filled item.
            title = api_item.get("name") or "Unknown item"
            answer = messagebox.askyesno(
                "Found online",
                f"Found '{title}' from UPCitemdb.\n"
                "Do you want to save it to your database?",
            )
            if answer:
                self.db.upsert_item(
                    api_item["barcode"],
                    api_item["name"],
                    api_item["description"],
                    api_item["image_path"],
                )
                saved = self.db.get_item(api_item["barcode"])
                if saved:
                    self._display_item(saved)
                    self.root.bell()
                    return
            # If user says No, continue to manual-add prompt below.

        # 3) Fallback: manual add.
        answer = messagebox.askyesno(
            "Unknown item",
            f"Barcode {barcode} not found.\nDo you want to add it?",
        )
        if answer:
            self._populate_add_form(barcode)
            self._show_add_view()

    def _ensure_barcode_focus(self) -> None:
        """
        Periodically refocus the barcode entry.

        This helps ensure the scanner input always goes to the correct
        field, even if a child clicks elsewhere.
        """
        try:
            if self.scan_frame.winfo_ismapped():
                self.barcode_entry.focus_set()
        except tk.TclError:
            # Window closing or other issue; ignore.
            pass
        self.root.after(1000, self._ensure_barcode_focus)

    # ------------------------------------------------------------------
    # UPCitemdb integration
    # ------------------------------------------------------------------

    def _lookup_upcitemdb(
        self,
        barcode: str,
    ) -> Optional[Dict[str, Optional[str]]]:
        """
        Lookup a barcode using the UPCitemdb trial API.

        Returns a dict with keys:
            barcode, name, description, image_path
        or None if not found or on error.

        Uses:
          - ean as barcode (falls back to scanned barcode)
          - title as name
          - images[0] as source image, which we download locally
        """
        params = {"upc": barcode}
        try:
            resp = requests.get(
                UPCITEMDB_URL,
                params=params,
                timeout=5,
            )
        except requests.RequestException as exc:
            print(f"UPCitemdb request error: {exc}")
            return None

        if resp.status_code != 200:
            print(f"UPCitemdb HTTP {resp.status_code}: {resp.text}")
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            print(f"UPCitemdb JSON parse error: {exc}")
            return None

        if data.get("code") != "OK":
            print(f"UPCitemdb response code not OK: {data}")
            return None

        total = data.get("total", 0)
        if not total:
            print("UPCitemdb: no items found")
            return None

        items = data.get("items") or []
        if not items:
            print("UPCitemdb: empty items list")
            return None

        item = items[0]

        # Extract the pieces you care about.
        ean = str(item.get("ean") or barcode)
        title = item.get("title") or ean
        description = item.get("description") or ""
        images = item.get("images") or []
        image_url = images[0] if images else None

        local_image_path: Optional[str] = None
        if image_url:
            local_image_path = self._download_image_from_url(image_url, ean)

        return {
            "barcode": ean,
            "name": title,
            "description": description,
            "image_path": local_image_path,
        }

    def _download_image_from_url(
        self,
        url: str,
        barcode: str,
    ) -> Optional[str]:
        """
        Download an image from a URL into the images folder.

        Uses the barcode as the filename (e.g., images/<barcode>.jpg).
        Returns the local file path or None on failure.
        """
        try:
            resp = requests.get(url, timeout=10)
        except requests.RequestException as exc:
            print(f"Image download error: {exc}")
            return None

        if resp.status_code != 200:
            print(
                f"Image download HTTP {resp.status_code}: "
                f"{url}"
            )
            return None

        # Very basic guess: just use .jpg for these API images.
        filename = f"{barcode}.jpg"
        file_path = os.path.join(IMAGES_DIR, filename)

        try:
            with open(file_path, "wb") as f:
                f.write(resp.content)
        except OSError as exc:
            print(f"Failed to save image {file_path}: {exc}")
            return None

        return file_path

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _display_item(self, item: Dict[str, Optional[str]]) -> None:
        """Update the scan view with the given item."""
        name = item.get("name") or "Unknown"
        description = item.get("description") or ""
        image_path = item.get("image_path") or PLACEHOLDER_IMAGE

        self.item_name_label.config(text=name)
        self.item_desc_text.delete("1.0", tk.END)
        self.item_desc_text.insert("1.0", description)

        self._show_image(image_path)

    def _show_image(self, path: str) -> None:
        """Load and display an image scaled to the right panel."""
        if not os.path.isfile(path):
            path = NO_IMAGE_IMAGE

        try:
            img = Image.open(path)
            img.thumbnail((self.img_max_w, self.img_max_h))
            self.current_image_tk = ImageTk.PhotoImage(img)
            self.item_image_label.config(image=self.current_image_tk)
        except Exception as exc:
            print(f"Failed to load image: {exc}")
            self.item_image_label.config(image="")
            self.current_image_tk = None

    def _clear_display(self) -> None:
        """Reset the scan view to the default message and image."""
        self.item_name_label.config(text="Scan an item to begin")
        self.item_desc_text.delete("1.0", tk.END)
        self.last_barcode_label.config(text="")
        self._show_image(PLACEHOLDER_IMAGE)

    # ------------------------------------------------------------------
    # Add/edit helpers
    # ------------------------------------------------------------------

    def _choose_image_file(self) -> None:
        """Open a file dialog to choose an image file."""
        initial_dir = os.path.abspath(IMAGES_DIR)
        if not os.path.isdir(initial_dir):
            initial_dir = os.getcwd()

        file_path = filedialog.askopenfilename(
            title="Choose Item Image",
            initialdir=initial_dir,
            filetypes=[
                ("Image files", "*.png;*.jpg;*.jpeg;*.gif;*.bmp"),
                ("All files", "*.*"),
            ],
        )
        if file_path:
            self.add_image_var.set(file_path)

    def _save_item(self) -> None:
        """Validate and save the item from the add/edit form."""
        barcode = self.add_barcode_var.get().strip()
        name = self.add_name_var.get().strip()
        description = self.add_desc_text.get("1.0", tk.END).strip()
        image_path = self.add_image_var.get().strip() or None

        if not barcode or not name:
            messagebox.showerror(
                "Missing data",
                "Barcode and name are required.",
            )
            return

        self.db.upsert_item(barcode, name, description, image_path)
        messagebox.showinfo("Saved", f"Item for barcode {barcode} saved.")

        item = self.db.get_item(barcode)
        if item:
            self.show_scan_view()
            self._display_item(item)

    # ------------------------------------------------------------------
    # Fullscreen helpers
    # ------------------------------------------------------------------

    def _toggle_fullscreen(self, event: Optional[tk.Event] = None) -> None:
        """Toggle fullscreen mode (F11)."""
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def _exit_fullscreen(self, event: Optional[tk.Event] = None) -> None:
        """Exit fullscreen mode (Escape)."""
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)

    def _toggle_fullscreen_btn(self) -> None:
        """Toggle fullscreen from a button press."""
        self._toggle_fullscreen(None)


def main() -> None:
    """Entry point for the Kid Scanner application."""
    # Ensure images directory exists.
    if not os.path.isdir(IMAGES_DIR):
        os.makedirs(IMAGES_DIR, exist_ok=True)

    db = ItemDatabase(DB_PATH)

    root = tk.Tk()
    app = KidScannerApp(root, db)  # noqa: F841
    root.mainloop()


if __name__ == "__main__":
    main()
