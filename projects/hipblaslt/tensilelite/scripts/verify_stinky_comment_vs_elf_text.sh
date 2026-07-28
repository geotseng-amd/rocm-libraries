#!/bin/sh
# Compare STINKY_TOTAL_INST_BYTES in an emitted .s to the .text section size of the matching .o.
# No Python. Requires: readelf (binutils), grep, sed.
#
# Usage:
#   verify_stinky_comment_vs_elf_text.sh path/to/kernel.s path/to/kernel.o
#
# Exit 0 if sizes match (or if .s has no marker — exits 0 skip). Exit 1 on mismatch. Exit 2 on error.
#
# Example (tox / CI): after assembling,
#   sh "$TENSILELITE_ROOT/scripts/verify_stinky_comment_vs_elf_text.sh" "$S" "$O"

set -eu

# Wall-clock timing only (stderr); does not affect exit codes or comparisons.
_verify_t0=$(date +%s.%N 2>/dev/null || date +%s)
trap '
	_verify_t1=$(date +%s.%N 2>/dev/null || date +%s)
	awk -v a="$_verify_t0" -v b="$_verify_t1" "BEGIN { d = b + 0 - (a + 0); if (d < 0) d = 0; printf \"%s: wall time %.3fs\\n\", \"verify_stinky_comment_vs_elf_text\", d }" >&2
' EXIT

S=${1:-}
O=${2:-}
if [ -z "$S" ] || [ -z "$O" ]; then
	echo "usage: $0 <kernel.s> <kernel.o>" >&2
	exit 2
fi
if [ ! -f "$S" ] || [ ! -f "$O" ]; then
	echo "$0: missing file" >&2
	exit 2
fi

cost=$(grep -E 'STINKY_TOTAL_INST_BYTES:[[:space:]]*[0-9]+' "$S" 2>/dev/null | head -n1 | sed -n 's/.*STINKY_TOTAL_INST_BYTES:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
if [ -z "$cost" ]; then
	echo "$0: no STINKY_TOTAL_INST_BYTES in $S (Stinky path off or old emitter); skip"
	exit 0
fi

# GNU readelf -W -S: fields are [Nr] Name Type Addr Off Size ... — Size is column 7 when Name is .text.
hex=$(readelf -W -S "$O" 2>/dev/null | awk '$3 == ".text" {print $7; exit}')
if [ -z "$hex" ]; then
	echo "$0: could not find .text in $O" >&2
	exit 2
fi
# strip 0x if present
hex=${hex#0x}
text=$((0x$hex))

if [ "$cost" -eq "$text" ]; then
	echo "OK STINKY_TOTAL_INST_BYTES=$cost == ELF .text=$text ($O)"
	exit 0
fi

echo "MISMATCH STINKY_TOTAL_INST_BYTES=$cost (from $S) vs ELF .text=$text ($O) diff=$((text - cost))" >&2
exit 1
