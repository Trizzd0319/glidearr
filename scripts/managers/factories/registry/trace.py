# registry/trace.py
import inspect
import os

class RegistryTracer:
    DECORATOR_FILES = {"timing.py", "logger.py", "decorators.py", "registry.py", "base_manager.py"}

    @staticmethod
    def trace_real_caller():
        stack = inspect.stack()
        for i, frame in enumerate(stack):
            filename = os.path.basename(frame.filename).lower()
            if filename in RegistryTracer.DECORATOR_FILES:
                continue

            caller_file = frame.filename
            caller_line = frame.lineno
            caller_func = frame.function
            caller_self = frame.frame.f_locals.get("self")
            caller_class = caller_self.__class__.__name__ if caller_self else "unknown"

            if caller_func == "__init__" and not caller_class.endswith("Manager"):
                for upper_frame in stack[i + 1:i + 5]:
                    upper_file = os.path.basename(upper_frame.filename).lower()
                    if upper_file in RegistryTracer.DECORATOR_FILES:
                        continue
                    upper_self = upper_frame.frame.f_locals.get("self")
                    upper_class = upper_self.__class__.__name__ if upper_self else "unknown"
                    upper_func = upper_frame.function
                    if upper_class.endswith("Manager") and upper_func != "__init__":
                        return upper_frame.filename, upper_frame.lineno, upper_func, upper_class
                return caller_file, caller_line, caller_func, caller_class

            return caller_file, caller_line, caller_func, caller_class

        return "unknown", "?", "unknown", "unknown"