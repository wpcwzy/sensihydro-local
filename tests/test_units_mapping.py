"""Guard against the unit<->slot mapping regressing.

The slot numbers below were verified on real hardware by reading the unique
identity code (state block +2/+3/+4) of every indoor board and matching it with
the per-unit identity codes reported by the vendor cloud.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import units

EXPECTED = {
    "餐厅": (1, 0x00C8, 0x03E8),
    "三楼南": (2, 0x00DC, 0x03F7),
    "二楼南": (4, 0x0104, 0x0415),
    "三楼北": (5, 0x0118, 0x0424),
    "客厅": (6, 0x012C, 0x0433),
    "二楼北": (7, 0x0140, 0x0442),
}


def main() -> int:
    ok = True
    for name, (slot, state_base, ctrl_base) in EXPECTED.items():
        unit = units.UNITS.get(name)
        if unit is None:
            print("FAIL missing unit", name); ok = False; continue
        if (unit.sequence, unit.state_base, unit.ctrl_base) != (slot, state_base, ctrl_base):
            print("FAIL", name, (unit.sequence, unit.state_base, unit.ctrl_base),
                  "!=", (slot, state_base, ctrl_base))
            ok = False
    print("UNIT MAPPING TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
