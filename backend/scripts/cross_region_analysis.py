import csv
from collections import defaultdict
from pathlib import Path

csv_path = Path(__file__).resolve().parent.parent.parent / "Data" / "Delivery Data 6.22 to 6.26.csv"

shift_terminals = defaultdict(lambda: defaultdict(int))

with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        driver = row.get("driver", "").strip()
        terminal = row.get("TerminalName", "").strip()
        shift = row.get("Shift", "").strip()
        if driver and terminal and shift:
            shift_terminals[shift][terminal] += 1

print("REGION → TERMINAL USAGE (from actuals Jun 22-26)\n")
print(f"{'Region':<10} {'Terminal':<30} {'Count':<8}")
print(f"{'-'*10} {'-'*30} {'-'*8}")

for shift in sorted(shift_terminals.keys()):
    terminals = shift_terminals[shift]
    for terminal, count in sorted(terminals.items(), key=lambda x: -x[1]):
        print(f"{shift:<10} {terminal:<30} {count:<8}")
    print()

# Define which regions are compatible with each terminal (based on actuals)
TERMINAL_COMPATIBLE = {
    "Tyler Delek": {"ET-AM"},
    "Global Hearne": {"ET-AM"},
    "Sunoco Caddo LLC": {"TX-AM"},
    "US OIL Melissa": {"TX-AM", "TX-PM", "TX-AM1"},
    "Dallas Magellan": {"TX-AM", "TX-PM", "TX-AM1", "FW-AM", "FW-PM"},
    "Dallas Motiva": {"TX-AM", "TX-PM", "FW-PM"},
    "Motiva Enterprises LLC": {"TX-AM", "TX-PM", "FW-AM", "FW-PM", "ET-AM", "TX-AM1", "FW-AM1"},
    "Irving Exxon": {"TX-AM", "TX-PM", "FW-AM", "FW-PM", "FW-AM1"},
    "Global Dallas": {"TX-AM", "TX-PM", "FW-AM", "FW-PM", "TX-AM1", "FW-AM1"},
    "Euless Flint Hills": {"FW-AM", "FW-PM", "TX-PM", "TX-AM", "TX-AM1", "FW-AM1"},
    "Ft Worth Motiva": {"FW-AM", "FW-PM", "TX-PM", "FW-AM1", "TX-AM"},
    "Ft Worth Chevron": {"FW-AM", "FW-PM", "FW-AM1"},
    "Southlake Nustar": {"FW-AM", "FW-PM", "TX-AM", "TX-PM", "FW-AM1"},
    "Musket": {"FW-AM", "FW-AM1"},
    "Cresson": {"FW-AM", "TX-AM"},
    "Euless Kinder Morgan": {"FW-AM", "TX-AM"},
    "Waco Flint HIlls": {"FW-AM", "FW-AM1"},
    "Waco Motiva": {"FW-AM"},
    "Aledo Magellan": {"FW-AM", "FW-PM"},
    "Direct Fuels LLC": {"FW-AM", "TX-PM"},
}

# Find cross-region assignments
violations = []
total = 0
with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        driver = row.get("driver", "").strip()
        terminal = row.get("TerminalName", "").strip()
        shift = row.get("Shift", "").strip()
        if not driver or not terminal or not shift:
            continue
        total += 1
        compatible = TERMINAL_COMPATIBLE.get(terminal)
        if compatible and shift not in compatible:
            violations.append((driver, shift, terminal))

print(f"\n{'='*70}")
print(f"CROSS-REGION ANALYSIS")
print(f"{'='*70}")
print(f"\nTotal deliveries analyzed: {total}")
print(f"Cross-region assignments: {len(violations)} ({len(violations)/max(total,1)*100:.1f}%)")

if violations:
    print(f"\n{'Driver':<25} {'Region':<10} {'Terminal':<30}")
    print(f"{'-'*25} {'-'*10} {'-'*30}")
    for driver, shift, terminal in violations[:30]:
        print(f"{driver:<25} {shift:<10} {terminal:<30}")
    if len(violations) > 30:
        print(f"  ... and {len(violations)-30} more")
else:
    print("\nNo cross-region violations found — all drivers stay within their region's terminals.")
