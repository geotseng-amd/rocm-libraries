################################################################################
#
# Copyright (C) 2025 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
################################################################################

"""Validate matrix_a_reuse / matrix_b_reuse on WMMA/MFMA instructions in AMDGPU asm.

rocisa emits:  <inst> <acc>, <a>, <b>, <acc2>  [matrix_a_reuse] [matrix_b_reuse]

Reuse flags apply to the *next* WMMA/MFMA in program order:
  matrix_a_reuse on line N  => line N+1 <a> must equal line N <a>  (error if not)
  matrix_b_reuse on line N  => line N+1 <b> must equal line N <b>  (error if not)

Repeated operands without a reuse flag are not required; they emit a warning only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

INST_HEAD_RE = re.compile(
    r"^\s*((?:v|s)_(?:wmma|mfma|smfma|mxmfma)[\w_.]*)\s+",
    re.IGNORECASE,
)
VREG_OPERAND_RE = re.compile(r"v\[[^\]]+\]")
REUSE_A_RE = re.compile(r"\bmatrix_a_reuse\b")
REUSE_B_RE = re.compile(r"\bmatrix_b_reuse\b")


@dataclass(frozen=True)
class MmaInsn:
    line_no: int
    line: str
    mnemonic: str
    acc: str
    a: str
    b: str
    acc2: str
    reuse_a: bool
    reuse_b: bool


@dataclass
class Issue:
    line_no: int  # cause line (the insn that sets the reuse flag)
    severity: str  # "error" | "warning"
    message: str
    line: str  # full text of cause line


def _strip_comment(line: str) -> str:
    if "//" in line:
        return line.split("//", 1)[0]
    return line


def parse_mma_line(line: str, line_no: int) -> Optional[MmaInsn]:
    body = _strip_comment(line).strip()
    if not body:
        return None
    m = INST_HEAD_RE.match(body)
    if not m:
        return None

    operands = VREG_OPERAND_RE.findall(body)
    if len(operands) < 4:
        return None

    return MmaInsn(
        line_no=line_no,
        line=line.rstrip("\n"),
        mnemonic=m.group(1).lower(),
        acc=operands[0],
        a=operands[1],
        b=operands[2],
        acc2=operands[3],
        reuse_a=bool(REUSE_A_RE.search(body)),
        reuse_b=bool(REUSE_B_RE.search(body)),
    )


def _reuse_mismatch_issue(
    cause: MmaInsn,
    nxt: MmaInsn,
    slot: str,
    expected: str,
    actual: str,
    flag_name: str,
) -> Issue:
    return Issue(
        line_no=cause.line_no,
        severity="error",
        message=(
            f"{flag_name} at cause line {cause.line_no}: next WMMA at line {nxt.line_no} "
            f"must reuse <{slot}> from cause line\n"
            f"  cause L{cause.line_no} <{slot}>: {expected}\n"
            f"  next  L{nxt.line_no} <{slot}>: {actual}\n"
            f"  cause: {cause.line.strip()}\n"
            f"  next:  {nxt.line.strip()}"
        ),
        line=cause.line,
    )


def _missing_reuse_issue(
    cause: MmaInsn,
    nxt: MmaInsn,
    slot: str,
    flag_name: str,
) -> Issue:
    return Issue(
        line_no=cause.line_no,
        severity="warning",
        message=(
            f"line {nxt.line_no} <{slot}> matches line {cause.line_no} but "
            f"{flag_name} is not set on cause line\n"
            f"  cause L{cause.line_no} <{slot}>: {cause.a if slot == 'a' else cause.b}\n"
            f"  next  L{nxt.line_no} <{slot}>: {nxt.a if slot == 'a' else nxt.b}\n"
            f"  cause: {cause.line.strip()}\n"
            f"  next:  {nxt.line.strip()}"
        ),
        line=cause.line,
    )


def check_asm_lines(
    lines: Iterable[str],
) -> Tuple[List[MmaInsn], List[Issue]]:
    insns: List[MmaInsn] = []
    issues: List[Issue] = []

    for line_no, raw in enumerate(lines, start=1):
        insn = parse_mma_line(raw, line_no)
        if insn is not None:
            insns.append(insn)

    for i, cause in enumerate(insns):
        if i + 1 >= len(insns):
            if cause.reuse_a or cause.reuse_b:
                flags = []
                if cause.reuse_a:
                    flags.append("matrix_a_reuse")
                if cause.reuse_b:
                    flags.append("matrix_b_reuse")
                issues.append(
                    Issue(
                        line_no=cause.line_no,
                        severity="warning",
                        message=(
                            f"{' '.join(flags)} at line {cause.line_no} but no following "
                            f"WMMA/MFMA to verify"
                        ),
                        line=cause.line,
                    )
                )
            continue

        nxt = insns[i + 1]

        if cause.reuse_a and nxt.a != cause.a:
            issues.append(
                _reuse_mismatch_issue(
                    cause, nxt, "a", cause.a, nxt.a, "matrix_a_reuse"
                )
            )
        if cause.reuse_b and nxt.b != cause.b:
            issues.append(
                _reuse_mismatch_issue(
                    cause, nxt, "b", cause.b, nxt.b, "matrix_b_reuse"
                )
            )

        if nxt.a == cause.a and not cause.reuse_a:
            issues.append(
                _missing_reuse_issue(cause, nxt, "a", "matrix_a_reuse")
            )
        if nxt.b == cause.b and not cause.reuse_b:
            issues.append(
                _missing_reuse_issue(cause, nxt, "b", "matrix_b_reuse")
            )

    return insns, issues


def verify_wmma_reuse_file(
    path: Union[str, Path],
) -> Tuple[List[MmaInsn], List[Issue]]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return check_asm_lines(text.splitlines())


def format_report(
    path_label: str,
    insns: List[MmaInsn],
    issues: List[Issue],
    *,
    verbose: bool = False,
) -> str:
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    out: List[str] = []
    out.append(f"=== check_wmma_reuse: {path_label} ===")
    out.append(f"WMMA/MFMA instructions: {len(insns)}")
    out.append(f"Errors: {len(errors)}  Warnings: {len(warnings)}")
    out.append("")

    if verbose and insns:
        out.append("--- instruction trace (cause line: acc, a, b, flags) ---")
        for insn in insns:
            flags = []
            if insn.reuse_a:
                flags.append("matrix_a_reuse->next.a")
            if insn.reuse_b:
                flags.append("matrix_b_reuse->next.b")
            flag_s = " ".join(flags) if flags else "(no reuse)"
            out.append(
                f"  L{insn.line_no:5d} {insn.mnemonic}  "
                f"a={insn.a}  b={insn.b}  [{flag_s}]"
            )
        out.append("")

    for issue in issues:
        prefix = "ERROR" if issue.severity == "error" else "WARN"
        out.append(f"{prefix} cause line {issue.line_no}:")
        out.append(issue.message)
        out.append("")

    if not errors:
        if warnings:
            out.append(
                "OK: set reuse flags match the next WMMA/MFMA operands "
                f"({len(warnings)} warning(s))."
            )
        else:
            out.append("OK: set reuse flags match the next WMMA/MFMA operands.")
    return "\n".join(out)
