################################################################################
#
# Copyright (C) 2025 Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
################################################################################

"""Unit tests for CheckWMMAReuse integration in TensileCreateLibrary/Run.py."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from Tensile.Common import IsaVersion
from Tensile.Common.GlobalParameters import globalParameters, restoreDefaultGlobalParameters
from Tensile.TensileCreateLibrary import Run as tcl_run

_GFX1250 = IsaVersion(12, 5, 0)
_GFX950 = IsaVersion(9, 5, 0)

_VALID_ASM = """
v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[a+0:a+7], v[b+0:b+7], v[c+0:c+7] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+8:c+15], v[a+8:a+15], v[b+0:b+7], v[c+8:c+15]
"""

_BROKEN_ASM = """
v_wmma_f32_16x16x32_bf16 v[c+0:c+7], v[a+0:a+7], v[b+0:b+7], v[c+0:c+7] matrix_b_reuse
v_wmma_f32_16x16x32_bf16 v[c+8:c+15], v[a+8:a+15], v[b+8:b+15], v[c+8:c+15]
"""


@pytest.fixture(autouse=True)
def _reset_global_parameters():
    restoreDefaultGlobalParameters()
    yield
    restoreDefaultGlobalParameters()


class TestWmmaReuseVerifyWanted:
    def test_disabled_when_flag_off(self):
        globalParameters["CheckWMMAReuse"] = False
        assert not tcl_run._wmma_reuse_verify_wanted(_GFX1250)

    def test_requires_check_flag(self):
        globalParameters["CheckWMMAReuse"] = True
        assert not tcl_run._wmma_reuse_verify_wanted(_GFX950)

    def test_requires_gfx1250(self):
        globalParameters["CheckWMMAReuse"] = True
        assert tcl_run._wmma_reuse_verify_wanted(_GFX1250)

    def test_false_when_flag_off_on_gfx1250(self):
        globalParameters["CheckWMMAReuse"] = False
        assert not tcl_run._wmma_reuse_verify_wanted(_GFX1250)


class TestVerifyWmmaReuseAsm:
    def test_valid_asm_passes(self, tmp_path: Path):
        asm = tmp_path / "k.s"
        asm.write_text(_VALID_ASM.strip() + "\n", encoding="utf-8")
        tcl_run._verify_wmma_reuse_asm(asm, "k")

    def test_invalid_asm_exits(self, tmp_path: Path, monkeypatch):
        asm = tmp_path / "k.s"
        asm.write_text(_BROKEN_ASM.strip() + "\n", encoding="utf-8")
        captured: list[str] = []

        def fake_stinky_out(msg: str) -> None:
            captured.append(msg)

        def fake_print_exit(msg, exitCode=1):
            captured.append(msg)
            raise SystemExit(exitCode)

        monkeypatch.setattr(tcl_run, "_stinky_out", fake_stinky_out)
        monkeypatch.setattr(tcl_run, "printExit", fake_print_exit)
        with pytest.raises(SystemExit):
            tcl_run._verify_wmma_reuse_asm(asm, "k")
        text = "\n".join(captured)
        assert "cause line 1" in text
        assert "matrix_b_reuse" in text
        assert "next  L2" in text
