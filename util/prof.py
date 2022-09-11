
import atexit
import functools
import time

class ProfBlock:
    children = None
    start = None
    end = None
    label = None
    
    def __init__(self):
        self.children = []
    
    def print(self, indent: int = 0, suppress: bool = False) -> None:
        if not suppress:
            if self.end is None:
                print(" " * indent + f"{self.label}: {time.perf_counter() - self.start:0.2f}")
            else:
                print(" " * indent + f"{self.label}: {self.end - self.start:0.2f}")
            
        for child in self.children:
            child.print(indent + 2)

root = ProfBlock()
current_context = root

root.start = time.perf_counter()
root.label = "root"

def prof(func):
    @functools.wraps(func)
    def wrapper_timer(*args, **kwargs):
        with Context(func.__name__):
            return func(*args, **kwargs)

    return wrapper_timer

class Context:
    def __init__(self, label):
        self.prof = ProfBlock()
        self.prof.label = label
        
        self.parent = None
        
    def __enter__(self):
        global current_context
        
        # add our new context to the parent
        current_context.children += [self.prof]
        self.parent = current_context
        current_context = self.prof
        
        self.prof.start = time.perf_counter()
  
    def __exit__(self, exception_type, exception_value, exception_traceback):
        global current_context
    
        self.prof.end = time.perf_counter()
        current_context = self.parent
        
        print(f"Finished {self.prof.label}, {self.prof.end - self.prof.start:0.2f} seconds")

@atexit.register
def printall() -> None:
    print()
    print("========= Prof dump")
    root.print(suppress = True)
