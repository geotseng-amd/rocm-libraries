#!/usr/bin/env python3
"""ABS SW-instruction-prefetch plan checker (wait-on vs wait-off model + structural invariants)."""
import os, re, glob, json

CMP = os.environ.get("ABS_CMP", "/data0/comparison_output")
OUT = os.environ.get("ABS_OUT", "/data0/abs_identity")

P0 = 128 * 255          # 32640
SPACING = 32 * 128      # 4096
ICACHE = 65536
MAX_STATIC_N = (ICACHE - P0) // SPACING

def count_n(total):
    n = 0
    while P0 + n * SPACING < total:
        n += 1
    return n

def static_N(total, wait):
    bf = 20 + wait
    N = count_n(total)
    for _ in range(2):
        N = count_n(total + bf + N * 8)
    return min(N, MAX_STATIC_N)

MAX_COVER = 8 - 4

def compute_cover_n(boundary, wait):
    ub = 320 + 4 * wait
    if boundary < 0 or boundary <= P0:
        return 0
    raw = (boundary + ub - P0) // SPACING + 1
    return max(0, min(MAX_COVER, raw))

RE_PREVIEW = re.compile(r"planned insert sites \(phase 2 preview.*?P\(0\)=(\d+)\s+totalLayoutBytes=(\d+)")
RE_PHASE2  = re.compile(r"Phase 2 abs-static insert:\s*totalLayoutBytes=(\d+).*?N_prefetches=(\d+)")
RE_CAP     = re.compile(r"capping N from (\d+) to I-cache max (\d+)")
RE_COMPLETE= re.compile(r"complete: inserted (\d+) s_prefetch_inst")
RE_STATIC_NOOP = re.compile(r"no-op: totalLayoutBytes \((\d+)\) > I-cache limit")
RE_SITE_P  = re.compile(r"\[insert-site k=(\d+) P=(\d+).*?action=(PLAN_INSERT|SKIP)")
RE_D1_EMIT = re.compile(r"D1 emitted after \S+.*?armN=(\d+)\s+ladder\(([^)]*)\)"
                        r"(?:\s+\+CPcover\(coverN=(\d+)\s+boundary=(-?\d+)\))?")
RE_D1_SKIP = re.compile(r"D1 emit skip")

def main():
    os.makedirs(os.path.join(OUT, "fails"), exist_ok=True)
    statics = sorted(glob.glob(os.path.join(CMP, "*", "*", "sw_prefetch_abs_static_pass.txt")))
    summ = open(os.path.join(OUT, "SUMMARY.tsv"), "w")
    summ.write("config\tkernel\tregime\tverdict\ttotal_pre\tN_dump\tN_on\tN_off\tcoverN_on\tcoverN_off\tboundary\tarmN\tn_wait_static\tnotes\n")
    npass = nfail = 0
    for spath in statics:
        kdir = os.path.dirname(spath); kernel = os.path.basename(kdir); config = os.path.basename(os.path.dirname(kdir))
        txt = open(spath, encoding="utf-8", errors="replace").read()
        dpath = os.path.join(kdir, "sw_prefetch_abs_dynamic_pass.txt")
        dtxt = open(dpath, encoding="utf-8", errors="replace").read() if os.path.isfile(dpath) else ""
        notes = []; verdict = "PASS"
        mprev = RE_PREVIEW.search(txt); total = int(mprev.group(2)) if mprev else None
        mp2 = RE_PHASE2.search(txt); N_dump = int(mp2.group(2)) if mp2 else None
        mcap = RE_CAP.search(txt); cap_to = int(mcap.group(2)) if mcap else None
        dyn_regime = RE_STATIC_NOOP.search(txt) is not None
        n_wait = txt.count('"st.s_wait_xcnt"')
        N_on = N_off = coverN_on = coverN_off = boundary = armN = None

        offgrid = [(int(k), int(P), a) for k, P, a in RE_SITE_P.findall(txt) + RE_SITE_P.findall(dtxt) if (int(P) - P0) % SPACING != 0]
        if offgrid:
            verdict = "OFFGRID"; notes.append(f"offgrid={offgrid[:3]}")

        if dyn_regime:
            regime = "DYNAMIC"
            md = RE_D1_EMIT.search(dtxt)
            if RE_D1_SKIP.search(dtxt) and not md:
                notes.append("D1 skip (no dyn prefetch)")
            elif md:
                armN = int(md.group(1))
                coverN_dump = int(md.group(3)) if md.group(3) is not None else None
                boundary = int(md.group(4)) if md.group(4) is not None else None
                if boundary is not None:
                    coverN_on = compute_cover_n(boundary, 4); coverN_off = compute_cover_n(boundary, 0)
                    if coverN_dump is not None and coverN_on != coverN_dump:
                        verdict = "MODEL_MISMATCH"; notes.append(f"coverN_on{coverN_on}!=dump{coverN_dump}")
                    if coverN_on != coverN_off:
                        verdict = "PLAN_DIFF"; notes.append(f"coverN {coverN_off}->{coverN_on} straddle")
                else:
                    notes.append("ladder-only no cover")
            else:
                notes.append("no D1 emission parsed")
        else:
            regime = "STATIC"
            if total is not None:
                N_on = static_N(total, 4); N_off = static_N(total, 0)
                if N_dump is not None and N_on != N_dump and not (cap_to is not None and N_dump == cap_to):
                    verdict = "MODEL_MISMATCH"; notes.append(f"N_on{N_on}!=dump{N_dump}")
                if N_on != N_off:
                    verdict = "PLAN_DIFF"; notes.append(f"N {N_off}->{N_on} straddle")

        n_ins = RE_COMPLETE.search(txt)
        n_ins = int(n_ins.group(1)) if n_ins else (N_dump or 0)
        if regime == "STATIC" and n_ins and n_ins > 0 and n_wait == 0:
            verdict = "WAIT_MISSING"; notes.append("static prefetch but no s_wait_xcnt")

        if verdict == "PASS": npass += 1
        else:
            nfail += 1
            json.dump(dict(config=config, kernel=kernel, regime=regime, verdict=verdict, total=total,
                           N_on=N_on, N_off=N_off, coverN_on=coverN_on, coverN_off=coverN_off,
                           boundary=boundary, armN=armN, notes=notes),
                      open(os.path.join(OUT, "fails", f"{config}__{kernel}.json"), "w"), indent=2)
        summ.write(f"{config}\t{kernel}\t{regime}\t{verdict}\t{total}\t{N_dump}\t{N_on}\t{N_off}\t"
                   f"{coverN_on}\t{coverN_off}\t{boundary}\t{armN}\t{n_wait}\t{';'.join(notes)}\n")
    summ.close()
    print(f"kernels={len(statics)} PASS={npass} NON_PASS={nfail}")

if __name__ == "__main__":
    main()
