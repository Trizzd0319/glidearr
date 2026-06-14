import os
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]  # Adjust for /scripts/managers/services/
SERVICES_DIR = Path(__file__).resolve().parent
TARGET_SERVICES = {"sonarr", "radarr", "trakt", "tautulli"}
CLASS_DEF_REGEX = re.compile(r"^class\s+([A-Z][A-Za-z0-9_]+)\s*\(", re.MULTILINE)

def camel_to_snake(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()

def update_imports_in_file(file_path, rename_map):
    with open(file_path, "r", encoding="utf-8") as f:
        contents = f.read()

    for original, updated in rename_map.items():
        # Replace import and usage references
        contents = re.sub(rf'\b{original}\b', updated, contents)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(contents)

def rename_classes_and_refs():
    print("🔍 Scanning for service classes to refactor...\n")
    rename_map = {}  # original_class → new_class

    for service in TARGET_SERVICES:
        service_dir = SERVICES_DIR / service
        for py_file in service_dir.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue

            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()

            updated_content = content
            matches = CLASS_DEF_REGEX.findall(content)

            for cls_name in matches:
                if not cls_name.endswith("Manager"):
                    new_name = cls_name + "Manager"
                    print(f"🛠️  Renaming class: {cls_name} → {new_name} in {py_file.relative_to(ROOT_DIR)}")
                    updated_content = re.sub(rf'\bclass\s+{cls_name}\b', f'class {new_name}', updated_content)
                    rename_map[cls_name] = new_name

            # Only write if changed
            if updated_content != content:
                with open(py_file, "w", encoding="utf-8") as f:
                    f.write(updated_content)

    # 🔄 Now update references across all service modules
    for service in TARGET_SERVICES:
        for py_file in (SERVICES_DIR / service).rglob("*.py"):
            update_imports_in_file(py_file, rename_map)

    print("\n✅ Refactor complete.")
    if not rename_map:
        print("🟢 No missing 'Manager' suffixes found.")

if __name__ == "__main__":
    rename_classes_and_refs()
