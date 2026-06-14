import ast
import os

class JsonDumpChecker(ast.NodeVisitor):
    def __init__(self):
        self.issues = []

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "dump" and isinstance(node.func.value, ast.Name) and node.func.value.id == "json":
                args = node.args
                keywords = {kw.arg for kw in node.keywords}
                needs_safe_wrap = True
                needs_default = "default" not in keywords

                # Check if data is already wrapped with make_json_safe(...)
                if args and isinstance(args[0], ast.Call):
                    func = args[0].func
                    if isinstance(func, ast.Name) and func.id == "make_json_safe":
                        needs_safe_wrap = False

                if needs_safe_wrap or needs_default:
                    self.issues.append((node.lineno, needs_safe_wrap, needs_default))
        self.generic_visit(node)


def check_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=filepath)
    checker = JsonDumpChecker()
    checker.visit(tree)

    if checker.issues:
        print(f"\n🔍 Issues in {filepath}:")
        for lineno, wrap, default in checker.issues:
            fixes = []
            if wrap:
                fixes.append("wrap with make_json_safe(...)")
            if default:
                fixes.append("add default=...")
            print(f"  Line {lineno}: {' and '.join(fixes)}")


def scan_project(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                check_file(os.path.join(root, file))


if __name__ == "__main__":
    # Scan this repo's `scripts/` tree, resolved relative to this file's location
    # (scripts/support/config/tvdb_test.py -> three levels up is `scripts/`).
    scripts_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    scan_project(scripts_root)
