import os
from typing import Dict, List, Tuple

def axis_to_4k(x:int) -> int:
    if x == 64:
        return 0
    elif x == 192:
        return 1
    elif x == 320:
        return 2
    else:
        return 3