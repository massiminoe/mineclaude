"""Minescript API discovery — run as \\api_probe in game."""
import minescript
import inspect

print("=== Minescript API Probe ===")
for name in sorted(dir(minescript)):
    if name.startswith("_"):
        continue
    obj = getattr(minescript, name)
    kind = type(obj).__name__
    if callable(obj):
        try:
            sig = str(inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(?)"
        print(f"  {name}{sig}  [{kind}]")
    else:
        print(f"  {name} = {obj!r}  [{kind}]")
print("=== End Probe ===")
