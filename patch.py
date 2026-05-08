# patch.py
import sys
from types import ModuleType

class AudioopModule(ModuleType):
    def __getattr__(self, name):
        if name == 'add':
            return lambda *args: bytes(0)
        elif name == 'mul':
            return lambda *args: bytes(0)
        elif name == 'ratecv':
            return lambda *args: (bytes(0), None)
        elif name == 'cross':
            return lambda *args: 0
        elif name == 'findmax':
            return lambda *args: (0, 0)
        elif name == 'max':
            return lambda *args: 0
        elif name == 'maxpp':
            return lambda *args: 0
        elif name == 'minmax':
            return lambda *args: (0, 0)
        elif name == 'avg':
            return lambda *args: 0
        elif name == 'avgpp':
            return lambda *args: 0
        elif name == 'rms':
            return lambda *args: 0
        elif name == 'bias':
            return lambda *args: bytes(0)
        elif name == 'reverse':
            return lambda *args: bytes(0)
        elif name == 'lin2lin':
            return lambda *args: bytes(0)
        elif name == 'adpcm2lin':
            return lambda *args: (bytes(0), None)
        elif name == 'lin2adpcm':
            return lambda *args: (bytes(0), None)
        elif name == 'ulaw2lin':
            return lambda *args: bytes(0)
        elif name == 'lin2ulaw':
            return lambda *args: bytes(0)
        raise AttributeError(f"audioop.{name}")
    
    def __init__(self):
        super().__init__('audioop')

sys.modules['audioop'] = AudioopModule()