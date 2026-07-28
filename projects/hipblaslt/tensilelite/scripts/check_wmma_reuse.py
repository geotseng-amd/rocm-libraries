#!/usr/bin/env python3
"""CLI wrapper for Tensile.verify_wmma_reuse (see that module for semantics)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from Tensile.verify_wmma_reuse import check_asm_lines, format_report, verify_wmma_reuse_file

_USER_SAMPLE = r"""
v_wmma_f32_16x16x32_bf16 v[vgprValuC+0:vgprValuC+0+7], v[vgprValuB_X0_I0+0+0+0-512:vgprValuB_X0_I0+0+0+0-512+7], v[vgprValuA_X0_I0+0+0+0-512:vgprValuA_X0_I0+0+0+0-512+7], v[vgprValuC+0:vgprValuC+0+7] matrix_a_reuse
v_wmma_f32_16x16x32_bf16 v[vgprValuC+8:vgprValuC+8+7], v[vgprValuB_X0_I0+0+0+0-512:vgprValuB_X0_I0+0+0+0-512+7], v[vgprValuA_X0_I0+8+0+0-512:vgprValuA_X0_I0+8+0+0-512+7], v[vgprValuC+8:vgprValuC+8+7] matrix_a_reuse
v_wmma_f32_16x16x32_bf16 v[vgprValuC+56:vgprValuC+56+7], v[vgprValuB_X0_I0+0+0+0-512:vgprValuB_X0_I0+0+0+0-512+7], v[vgprValuA_X0_I0+56+0+0-512:vgprValuA_X0_I0+56+0+0-512+7], v[vgprValuC+56:vgprValuC+56+7]
"""


def _self_test() -> None:
    insns, issues = check_asm_lines(_USER_SAMPLE.strip().splitlines())
    assert len(insns) == 3, insns
    assert not any(i.severity == "error" for i in issues), issues

    broken = """
v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[a+0:a+7], v[b+0:b+7], v[c+0:c+7] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+8:c+15], v[a+8:a+15], v[b+8:b+15], v[c+8:c+15]
"""
    _, bad_issues = check_asm_lines(broken.strip().splitlines())
    assert any(i.severity == "error" for i in bad_issues), bad_issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify matrix_a_reuse / matrix_b_reuse operands in AMDGPU asm.",
    )
    parser.add_argument("inputs", nargs="+", help="Assembly file(s), or '-' for stdin")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    any_error = False
    for path_str in args.inputs:
        if path_str == "-":
            text = sys.stdin.read()
            label = "<stdin>"
            insns, issues = check_asm_lines(text.splitlines())
        else:
            path = Path(path_str)
            if not path.is_file():
                print(f"ERROR: not a file: {path}", file=sys.stderr)
                any_error = True
                continue
            label = str(path)
            insns, issues = verify_wmma_reuse_file(path)

        report = format_report(label, insns, issues, verbose=args.verbose)
        if issues or not args.quiet:
            print(report)
        elif args.quiet:
            print(f"OK {label}: {len(insns)} WMMA/MFMA")

        if any(i.severity == "error" for i in issues):
            any_error = True

    return 1 if any_error else 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        _self_test()
        print("self-test passed")
        sys.exit(0)
    sys.exit(main())
