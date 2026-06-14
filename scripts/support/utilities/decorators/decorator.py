import os
import re

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


@LoggerManager().log_function_entry
@timeit("add_decorators_to_file")
def add_decorators_to_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    updated_lines = []
    func_pattern = re.compile(r'^(\s*)def\s+(\w+)\(')

    i = 0
    while i < len(lines):
        line = lines[i]
        match = func_pattern.match(line)

        if match:
            indent = match.group(1)
            func_name = match.group(2)

            # Scan previous 2 lines to check if decorators already exist
            decorators_present = {
                "logger": False,
                "timeit": False
            }

            if i > 0 and "@LoggerManager().log_function_entry" in lines[i - 1]:
                decorators_present["logger"] = True
            if i > 1 and "@LoggerManager().log_function_entry" in lines[i - 2]:
                decorators_present["logger"] = True
            if i > 0 and "@timeit" in lines[i - 1]:
                decorators_present["timeit"] = True
            if i > 1 and "@timeit" in lines[i - 2]:
                decorators_present["timeit"] = True

            if not decorators_present["logger"]:
                updated_lines.append(f"{indent}@LoggerManager().log_function_entry\n")
            if not decorators_present["timeit"]:
                updated_lines.append(f'{indent}@timeit("{func_name}")\n')

        updated_lines.append(line)
        i += 1

    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(updated_lines)

    print(f"✅ Updated {file_path}")


@LoggerManager().log_function_entry
@timeit("insert_timeit_import")
def insert_timeit_import(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    logger_line_index = None
    already_imported = any("from scripts.support.utilities.timing import timeit" in line for line in lines)

    if already_imported:
        return  # ✅ Already present

    for idx, line in enumerate(lines):
        if "from scripts.support.utilities.logger.logger import LoggerManager" in line:
            logger_line_index = idx
            break

    if logger_line_index is not None:
        lines.insert(logger_line_index, "from scripts.support.utilities.timing import timeit\n")

        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        print(f"🧩 Inserted `timeit` import in {file_path}")


@LoggerManager().log_function_entry
@timeit("process_directory")
def process_directory(root_dir):
    for subdir, _, files in os.walk(root_dir):
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(subdir, file)
                insert_timeit_import(full_path)
                add_decorators_to_file(full_path)


# Example usage
process_directory("/beta")
