"""Legacy compatibility exports for the shared caption format package."""
from anima_caption_format import flat_txt as _shared_flat_txt


for _name in dir(_shared_flat_txt):
    if _name not in {"__builtins__", "__cached__", "__file__", "__loader__", "__name__", "__package__", "__spec__"}:
        globals()[_name] = getattr(_shared_flat_txt, _name)
del _name
