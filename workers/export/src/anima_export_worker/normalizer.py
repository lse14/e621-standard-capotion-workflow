"""Legacy compatibility exports for the shared caption format package."""
from anima_caption_format import normalizer as _shared_normalizer


for _name in dir(_shared_normalizer):
    if _name not in {"__builtins__", "__cached__", "__file__", "__loader__", "__name__", "__package__", "__spec__"}:
        globals()[_name] = getattr(_shared_normalizer, _name)
del _name
