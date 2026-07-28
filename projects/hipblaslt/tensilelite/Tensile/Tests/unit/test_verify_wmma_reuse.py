################################################################################
#
# Copyright (C) 2025 Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
################################################################################

import pytest

pytestmark = pytest.mark.unit

from Tensile.verify_wmma_reuse import check_asm_lines, verify_wmma_reuse_file


def test_valid_matrix_b_reuse_next_line():
    """matrix_b_reuse on line N: line N+1 <b> must match line N <b>."""
    asm = """
v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[a+0:a+7], v[b+0:b+7], v[c+0:c+7] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+8:c+15], v[a+8:a+15], v[b+0:b+7], v[c+8:c+15]
v_wmma_f32_16x16x32_bf16 v[c+16:c+23], v[a+16:a+23], v[b+8:b+15], v[c+16:c+23] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+24:c+31], v[a+24:a+31], v[b+8:b+15], v[c+24:c+31]
"""
    insns, issues = check_asm_lines(asm.strip().splitlines())
    assert len(insns) == 4
    assert not [i for i in issues if i.severity == "error"]


def test_invalid_matrix_b_reuse_next_line_differs():
    asm = """
v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[a+0:a+7], v[b+0:b+7], v[c+0:c+7] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+8:c+15], v[a+8:a+15], v[b+8:b+15], v[c+8:c+15]
"""
    _, issues = check_asm_lines(asm.strip().splitlines())
    assert len(issues) == 1
    err = issues[0]
    assert err.severity == "error"
    assert err.line_no == 1  # cause line (first wmma with matrix_b_reuse)
    assert "cause line 1" in err.message
    assert "next  L2" in err.message
    assert "matrix_b_reuse" in err.message


def test_user_style_matrix_b_reuse():
    asm = """
v_wmma_f32_16x16x32_bf16 v[c+16:c+23], v[a+0:a+7], v[b+8:b+15], v[c+16:c+23] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+24:c+31], v[a+8:a+15], v[b+8:b+15], v[c+24:c+31]
"""
    insns, issues = check_asm_lines(asm.strip().splitlines())
    assert not [i for i in issues if i.severity == "error"]


def test_missing_matrix_a_reuse_when_operand_repeats_is_warning():
    asm = """
v_wmma_scale_f32_16x16x128_f8f6f4 v[c+0:c+7], v[a+0:a+15], v[b+0:b+15], v[c+0:c+7], v[mxsa+0], v[mxsb+0]
v_wmma_scale_f32_16x16x128_f8f6f4 v[c+64:c+71], v[a+0:a+15], v[b+16:b+31], v[c+64:c+71], v[mxsa+0], v[mxsb+1]
"""
    _, issues = check_asm_lines(asm.strip().splitlines())
    assert len(issues) == 1
    warn = issues[0]
    assert warn.severity == "warning"
    assert warn.line_no == 1
    assert "matrix_a_reuse is not set" in warn.message


def test_verify_file(tmp_path):
    p = tmp_path / "k.s"
    p.write_text(
        "v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[b+0:b+7], v[a+0:a+7], v[c+0:c+7]\n",
        encoding="utf-8",
    )
    insns, issues = verify_wmma_reuse_file(p)
    assert len(insns) == 1
    assert not issues
